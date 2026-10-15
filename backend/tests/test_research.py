import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import research as research_api
from app.core.config import Settings, get_settings
from app.main import app
from app.repositories.tasks import PersistenceError, SupabaseTaskRepository
from app.schemas.evidence import ExtractedField, ExtractionResult
from app.schemas.plan import ResearchPlan
from app.schemas.research import ResearchRequest
from app.services import research_workflow
from app.services.ollama import generate_structured
from app.services import planner as planner_service
from app.services.progress import ProgressHub, progress_hub
from app.services.url_safety import is_public_url, normalize_url
from app.tools.firecrawl import FirecrawlError, FirecrawlScraper
from app.tools.tavily import TavilyError, TavilySearch


def valid_plan():
    return ResearchPlan.model_validate({
        "topic": "Physiotherapy clinics",
        "summary": "Search clinic websites and extract prices.",
        "required_fields": ["name", "price"],
        "actions": [{
            "action_id": "search_clinics",
            "action_type": "web_search",
            "goal": "Find clinic pages",
            "query": "physiotherapy clinic consultation price Bangalore",
            "required_tool": "tavily_search",
            "expected_result": {"description": "Candidate clinic pages", "required_fields": ["name", "price"]},
            "verification_rule": "Retain source URL for each result",
        }],
    })


def test_plan_rejects_unknown_action_type():
    payload = valid_plan().model_dump()
    payload["actions"][0]["action_type"] = "run_python"
    with pytest.raises(ValidationError):
        ResearchPlan.model_validate(payload)


def test_extraction_requires_excerpt_and_no_value_when_missing():
    with pytest.raises(ValidationError):
        ExtractedField(entity_name="Clinic", field_name="price", field_value="₹500", value_status="found")
    with pytest.raises(ValidationError):
        ExtractedField(entity_name="Clinic", field_name="price", field_value="₹500", value_status="not_found")


def test_workflow_rejects_value_not_supported_by_excerpt():
    result = ExtractionResult(findings=[
        ExtractedField(entity_name="Clinic", field_name="price", field_value="₹900", value_status="found", supporting_excerpt="Consultation price: ₹500.")
    ])
    with pytest.raises(ValueError, match="not present in its supporting excerpt"):
        research_workflow._validate_extraction(result, ["price"], "Consultation price: ₹500.")


def test_request_rejects_unknown_properties():
    with pytest.raises(ValidationError):
        ResearchRequest(query="Find clinics", arbitrary_tool="unsafe")


def test_url_canonicalization_drops_tracking_and_rejects_non_http():
    assert normalize_url("HTTPS://Example.Org/path/?utm_source=search&ref=site#top") == "https://example.org/path?ref=site"
    with pytest.raises(ValueError):
        normalize_url("file:///C:/secret.txt")


@pytest.mark.asyncio
async def test_ssrf_screen_rejects_private_and_nonstandard_ports():
    assert await is_public_url("http://127.0.0.1/private") is False
    assert await is_public_url("https://example.org:8080/") is False


@pytest.mark.asyncio
async def test_tavily_search_mocked_http():
    async def handler(request):
        body = json.loads(request.content)
        assert body["query"] == "clinic pricing Bangalore"
        assert body["include_domains"] == ["example.org"]
        return httpx.Response(200, json={"results": [{"url": "https://example.org/prices", "title": "Price list"}]})

    result = await TavilySearch("placeholder", transport=httpx.MockTransport(handler)).search(
        "clinic pricing Bangalore", domain="example.org"
    )
    assert result[0]["title"] == "Price list"


@pytest.mark.asyncio
async def test_ollama_structured_plan_uses_configured_model_and_pydantic_schema():
    async def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert body["stream"] is False
        return httpx.Response(200, json={"response": valid_plan().model_dump_json()})

    settings = Settings(_env_file=None, ollama_base_url="http://ollama.test", ollama_model="test-model")
    result = await generate_structured(settings, system="system", prompt="prompt", schema=ResearchPlan, transport=httpx.MockTransport(handler))
    assert isinstance(result, ResearchPlan)
    assert result.actions[0].required_tool == "tavily_search"


@pytest.mark.asyncio
async def test_planner_normalizes_model_field_lists_to_validated_request_fields(monkeypatch):
    plan = valid_plan()
    plan.actions[0].expected_result.required_fields = ["name", ": ", "price"]

    async def fake_generate(*_args, **_kwargs):
        return plan

    monkeypatch.setattr(planner_service, "generate_structured", fake_generate)
    request = ResearchRequest(query="Find clinic name and consultation price", options={"required_fields": ["name", "price"]})
    normalized = await planner_service.create_plan(Settings(_env_file=None), request)
    assert normalized.actions[0].expected_result.required_fields == ["name", "price"]


@pytest.mark.asyncio
async def test_tavily_rate_limit_is_actionable():
    async def handler(_request):
        return httpx.Response(429, json={"error": "rate limit"})

    with pytest.raises(TavilyError, match="rate limited"):
        await TavilySearch("placeholder", transport=httpx.MockTransport(handler)).search("query")


@pytest.mark.asyncio
async def test_tavily_provider_auth_failure_is_safe_and_actionable():
    async def handler(_request):
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(TavilyError, match="401") as error:
        await TavilySearch("test-secret", transport=httpx.MockTransport(handler)).search("query")
    assert "test-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_tavily_rejects_malformed_response_object():
    async def handler(_request):
        return httpx.Response(200, json=["not", "an", "object"])

    with pytest.raises(TavilyError, match="invalid response object"):
        await TavilySearch("placeholder", transport=httpx.MockTransport(handler)).search("query")


@pytest.mark.asyncio
async def test_firecrawl_returns_page_content_and_metadata():
    async def handler(request):
        assert request.headers["Authorization"] == "Bearer placeholder"
        return httpx.Response(200, json={"success": True, "data": {"markdown": "Public source text", "metadata": {"title": "Source"}}})

    result = await FirecrawlScraper("placeholder", transport=httpx.MockTransport(handler)).scrape("https://example.org")
    assert result["markdown"] == "Public source text"
    assert result["metadata"]["title"] == "Source"


@pytest.mark.asyncio
async def test_firecrawl_handles_rate_limit_without_crashing_adapter():
    async def handler(_request):
        return httpx.Response(429, json={"error": "rate limit"})

    with pytest.raises(FirecrawlError, match="rate limited"):
        await FirecrawlScraper("placeholder", transport=httpx.MockTransport(handler)).scrape("https://example.org")


@pytest.mark.asyncio
async def test_firecrawl_provider_auth_failure_does_not_expose_key():
    async def handler(_request):
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(FirecrawlError, match="401") as error:
        await FirecrawlScraper("test-secret", transport=httpx.MockTransport(handler)).scrape("https://example.org")
    assert "test-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_firecrawl_rejects_malformed_response_object():
    async def handler(_request):
        return httpx.Response(200, json=["not", "an", "object"])

    with pytest.raises(FirecrawlError, match="invalid response object"):
        await FirecrawlScraper("placeholder", transport=httpx.MockTransport(handler)).scrape("https://example.org")


class MemoryRepository:
    def __init__(self):
        self.tasks = {}
        self.steps = {}
        self.sources = {}
        self.findings = {}

    def create_task(self, row):
        self.tasks[row["id"]] = dict(row)
        return self.tasks[row["id"]]

    def update_task(self, task_id, row):
        self.tasks[task_id].update(row)

    def create_step(self, row):
        row = dict(row)
        row.setdefault("id", str(uuid4()))
        self.steps[row["action_key"]] = row
        return row

    def update_step(self, _task_id, action_key, row):
        self.steps[action_key].update(row)

    def upsert_source(self, row):
        row = dict(row)
        row.setdefault("id", str(uuid4()))
        self.sources[row["canonical_url"]] = row
        return row

    def update_source(self, source_id, row):
        next(source for source in self.sources.values() if source["id"] == source_id).update(row)

    def upsert_findings(self, rows):
        result = []
        for row in rows:
            item = dict(row)
            item.setdefault("id", str(uuid4()))
            self.findings[(item["source_id"], item["field_name"])] = item
            result.append(item)
        return result

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def list_sources(self, task_id):
        return [row for row in self.sources.values() if row["task_id"] == task_id]

    def list_findings(self, task_id):
        return [row for row in self.findings.values() if row["task_id"] == task_id]


@pytest.mark.asyncio
async def test_workflow_searches_reads_extracts_and_streams_mocked_evidence(monkeypatch):
    request = ResearchRequest(query="Find clinic name and consultation price", options={"required_fields": ["name", "price"]})
    settings = Settings(_env_file=None, tavily_api_key="test", firecrawl_api_key="test", max_task_duration_seconds=15)
    repository = MemoryRepository()
    task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": request.query, "status": "QUEUED"})
    hub = ProgressHub()

    async def fake_plan(_settings, _request):
        return valid_plan()

    class FakeSearch:
        def __init__(self, *_args): pass
        async def search(self, *_args, **_kwargs):
            return [
                {"url": "https://example.org/prices?utm_source=search#top", "title": "Clinic prices"},
                {"url": "https://example.org/prices", "title": "Duplicate result"},
                {"url": "https://other.example.org/fail", "title": "Blocked page"},
            ]

    class FakeScraper:
        def __init__(self, *_args): pass
        async def scrape(self, _url):
            if "other.example.org" in _url:
                raise FirecrawlError("Firecrawl rate limited: 429", 429)
            return {"markdown": "Clinic: Alpha Care. Consultation price: ₹500.", "metadata": {"title": "Alpha Care fees"}}

    async def fake_extract(*_args, **_kwargs):
        return ExtractionResult(findings=[
            ExtractedField(entity_name="Alpha Care", field_name="name", field_value="Alpha Care", value_status="found", supporting_excerpt="Clinic: Alpha Care."),
            ExtractedField(entity_name="Alpha Care", field_name="price", field_value="₹500", value_status="found", supporting_excerpt="Consultation price: ₹500."),
        ])

    monkeypatch.setattr(research_workflow, "create_plan", fake_plan)
    monkeypatch.setattr(research_workflow, "TavilySearch", FakeSearch)
    monkeypatch.setattr(research_workflow, "FirecrawlScraper", FakeScraper)
    monkeypatch.setattr(research_workflow, "extract_fields", fake_extract)
    monkeypatch.setattr(research_workflow, "is_public_url", lambda _url: asyncio.sleep(0, result=True))

    await research_workflow.execute_research(task_id, request, settings, repository, hub)

    assert repository.tasks[task_id]["status"] == "COMPLETED_WITH_LIMITATIONS"
    assert len(repository.sources) == 2
    assert len(repository.findings) == 2
    assert all(item["verification_status"] == "source_supported" for item in repository.findings.values())
    event_types = [event["event"] for event in hub.history(task_id)]
    assert "source_discovered" in event_types
    assert "finding_extracted" in event_types
    assert "rate limited" in repository.tasks[task_id]["error_summary"]
    assert event_types[-1] == "task_completed"


def test_supabase_repository_crud_uses_expected_tables_and_upsert_keys():
    class Response:
        def __init__(self, data): self.data = data

    class Query:
        def __init__(self, client, table):
            self.client, self.name, self.operation = client, table, "select"
            self.payload, self.filters, self.conflict = None, [], None
        def insert(self, payload): self.operation, self.payload = "insert", payload; return self
        def upsert(self, payload, on_conflict=None): self.operation, self.payload, self.conflict = "upsert", payload, on_conflict; return self
        def update(self, payload): self.operation, self.payload = "update", payload; return self
        def select(self, *_args): return self
        def limit(self, _n): return self
        def order(self, *_args): return self
        def eq(self, field, value): self.filters.append((field, value)); return self
        def execute(self):
            table = self.client.tables.setdefault(self.name, [])
            if self.operation == "insert":
                items = self.payload if isinstance(self.payload, list) else [self.payload]
                for item in items:
                    item.setdefault("id", str(uuid4()))
                    table.append(dict(item))
                return Response([dict(item) for item in items])
            if self.operation == "upsert":
                items = self.payload if isinstance(self.payload, list) else [self.payload]
                output = []
                keys = self.conflict.split(",") if self.conflict else []
                for item in items:
                    existing = next((entry for entry in table if keys and all(entry.get(k) == item.get(k) for k in keys)), None)
                    if existing: existing.update(item); output.append(existing)
                    else:
                        item.setdefault("id", str(uuid4())); table.append(dict(item)); output.append(item)
                return Response([dict(item) for item in output])
            matched = [item for item in table if all(item.get(k) == v for k, v in self.filters)]
            if self.operation == "update":
                for item in matched: item.update(self.payload)
                return Response(matched)
            return Response(matched)

    class Client:
        def __init__(self): self.tables = {}
        def table(self, name): return Query(self, name)

    client = Client()
    repository = SupabaseTaskRepository("", "", client=client)
    task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": "Example", "status": "QUEUED"})
    repository.create_step({"task_id": task_id, "action_key": "search_1", "sequence_no": 1, "action_type": "web_search", "title": "Find pages"})
    repository.update_step(task_id, "search_1", {"status": "SUCCEEDED"})
    source = repository.upsert_source({"task_id": task_id, "canonical_url": "https://example.org/", "url": "https://example.org", "domain": "example.org", "fetch_status": "discovered"})
    repository.upsert_source({"task_id": task_id, "canonical_url": "https://example.org/", "url": "https://example.org", "domain": "example.org", "fetch_status": "retrieved"})
    repository.upsert_findings([{"task_id": task_id, "source_id": source["id"], "entity_key": "alpha", "field_name": "name", "value_status": "found"}])
    repository.update_task(task_id, {"status": "RUNNING"})

    assert len(client.tables["sources"]) == 1
    assert client.tables["sources"][0]["fetch_status"] == "retrieved"
    assert len(repository.list_findings(task_id)) == 1
    assert client.tables["research_steps"][0]["status"] == "SUCCEEDED"
    assert repository.get_task(task_id)["status"] == "RUNNING"


def test_supabase_pgrst205_includes_safe_schema_remediation():
    class FakePostgrestError(Exception):
        code = "PGRST205"

        def __str__(self):
            return "secret token and request URL must not be forwarded"

    class Query:
        def select(self, *_args): return self
        def eq(self, *_args): return self
        def limit(self, *_args): return self
        def execute(self): raise FakePostgrestError()

    class Client:
        def table(self, name):
            assert name == "research_tasks"
            return Query()

    repository = SupabaseTaskRepository("", "", client=Client())
    with pytest.raises(PersistenceError, match="PGRST205") as error:
        repository.get_task(str(uuid4()))
    assert error.value.provider_code == "PGRST205"
    assert "Apply the Stage 2 migration" in str(error.value)
    assert "schema cache" in str(error.value)
    assert "secret token" not in str(error.value)


def test_supabase_upserts_use_representation_without_select_chaining():
    class Builder:
        def __init__(self, payload): self.payload = payload
        def execute(self):
            rows = self.payload if isinstance(self.payload, list) else [self.payload]
            return type("Response", (), {"data": rows})()

    class Table:
        def __init__(self, name): self.name = name
        def upsert(self, payload, *, on_conflict):
            assert on_conflict
            return Builder(payload)

    class Client:
        def table(self, name): return Table(name)

    repository = SupabaseTaskRepository("", "", client=Client())
    source = repository.upsert_source({"id": "source-id", "task_id": "task-id", "canonical_url": "https://example.org"})
    findings = repository.upsert_findings([{"id": "finding-id", "task_id": "task-id", "source_id": "source-id"}])
    assert source["id"] == "source-id"
    assert findings[0]["id"] == "finding-id"


@pytest.mark.asyncio
async def test_tavily_failure_marks_search_step_and_finishes_with_limitations(monkeypatch):
    request = ResearchRequest(query="Find clinic name and consultation price", options={"required_fields": ["name", "price"]})
    settings = Settings(_env_file=None, tavily_api_key="test", firecrawl_api_key="test")
    repository = MemoryRepository()
    task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": request.query, "status": "QUEUED"})
    hub = ProgressHub()

    async def fake_plan(_settings, _request):
        return valid_plan()

    class BrokenSearch:
        def __init__(self, *_args): pass
        async def search(self, *_args, **_kwargs): raise TavilyError("Tavily search request failed: 401")

    monkeypatch.setattr(research_workflow, "create_plan", fake_plan)
    monkeypatch.setattr(research_workflow, "TavilySearch", BrokenSearch)
    await research_workflow.execute_research(task_id, request, settings, repository, hub)

    assert repository.tasks[task_id]["status"] == "COMPLETED_WITH_LIMITATIONS"
    assert repository.steps["search_clinics"]["status"] == "FAILED"
    assert "401" in repository.steps["search_clinics"]["last_error"]
    assert [event["event"] for event in hub.history(task_id)][-1] == "task_completed"


def test_repository_requires_service_role_config():
    with pytest.raises(PersistenceError, match="SUPABASE_URL"):
        SupabaseTaskRepository("", "")


@pytest.fixture
def api_memory_repository():
    repository = MemoryRepository()
    app.dependency_overrides[research_api.get_repository] = lambda: repository
    yield repository
    app.dependency_overrides.clear()


def test_research_api_reports_missing_provider_credentials():
    settings = Settings(_env_file=None)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = TestClient(app).post("/api/research", json={"query": "Find clinic pricing"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "providers_not_configured"
    assert {"SUPABASE_URL", "TAVILY_API_KEY", "FIRECRAWL_API_KEY"}.issubset(set(response.json()["detail"]["missing"]))


def test_research_api_persists_and_accepts_valid_request(monkeypatch):
    settings = Settings(
        _env_file=None,
        supabase_url="https://project.example",
        supabase_service_role_key="server-test-secret",
        tavily_api_key="test-key",
        firecrawl_api_key="test-key",
    )
    repository = MemoryRepository()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[research_api.get_repository] = lambda: repository

    async def ready(_settings):
        return {"connected": True, "model_available": True}

    async def fake_background(task_id, _body, _settings, _repository, hub):
        await hub.publish(task_id, "task_status", {"task_id": task_id, "status": "PLANNING"})

    monkeypatch.setattr(research_api, "check_ollama", ready)
    monkeypatch.setattr(research_api, "execute_research", fake_background)
    try:
        response = TestClient(app).post("/api/research", json={"query": "Find clinic consultation prices"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "QUEUED"
    assert payload["events_url"].endswith(f"{payload['task_id']}/events")
    assert repository.tasks[payload["task_id"]]["query"] == "Find clinic consultation prices"
    assert progress_hub.history(payload["task_id"])[0]["event"] == "task_status"


def test_research_api_validates_request_shape():
    settings = Settings(_env_file=None)
    app.dependency_overrides[research_api.require_research_providers] = lambda: settings
    app.dependency_overrides[research_api.get_repository] = lambda: MemoryRepository()
    try:
        response = TestClient(app).post("/api/research", json={"query": "x"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 422


def test_sse_replays_real_progress_and_terminal_event(api_memory_repository):
    task_id = str(uuid4())
    api_memory_repository.create_task({"id": task_id, "query": "Find clinic pricing", "status": "RUNNING"})
    asyncio.run(progress_hub.publish(task_id, "task_status", {"task_id": task_id, "status": "RUNNING"}))
    asyncio.run(progress_hub.publish(task_id, "task_completed", {"task_id": task_id, "status": "COMPLETED", "summary": "Finished"}))

    response = TestClient(app).get(f"/api/research/{task_id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: task_status" in response.text
    assert "event: task_completed" in response.text

