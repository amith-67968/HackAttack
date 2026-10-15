import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
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
from app.services import extraction as extraction_service
from app.services.ollama import OllamaError, generate_structured
from app.services import planner as planner_service
from app.services.progress import ProgressHub, progress_hub
from app.services.url_safety import is_public_url, normalize_url
from app.services.retry import classify_error, retry_delay, run_with_retries
from app.services.transitions import validate_task_completion, validate_transition
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


def test_unavailable_model_value_is_not_promoted_and_unsupported_claim_is_downgraded():
    from app.services.extraction import _normalize_model_output
    supported = {"findings": [{"field_value": "₹500", "value_status": "not_found", "supporting_excerpt": "Consultation price: ₹500."}]}
    normalized = _normalize_model_output(supported, "Fees\nConsultation price: ₹500.")
    assert normalized["findings"][0]["value_status"] == "not_found"
    assert normalized["findings"][0]["field_value"] is None
    unsupported = {"findings": [{"field_value": "₹900", "value_status": "not_found", "supporting_excerpt": "No price listed."}]}
    normalized = _normalize_model_output(unsupported, "Fees\nNo price listed.")
    assert normalized["findings"][0]["field_value"] is None
    assert normalized["findings"][0]["value_status"] == "not_found"
    stale_excerpt = {"findings": [{"field_value": "perseverance", "value_status": "found", "supporting_excerpt": "The name is not here."}]}
    normalized = _normalize_model_output(stale_excerpt, "NASA's Perseverance rover broke a record.")
    assert normalized["findings"][0]["field_value"] is None
    assert normalized["findings"][0]["value_status"] == "unverified"
    repeated = {"findings": [
        {"entity_name": "NASA rover", "field_name": "mission name", "field_value": "Perseverance", "value_status": "found", "supporting_excerpt": "Perseverance broke a record."},
        {"entity_name": "NASA rover", "field_name": "mission name", "field_value": "Perseverance", "value_status": "found", "supporting_excerpt": "NASA's Perseverance broke a record."},
    ]}
    assert len(_normalize_model_output(repeated, "NASA's Perseverance broke a record.")["findings"]) == 1


def test_workflow_rejects_value_not_supported_by_excerpt():
    result = ExtractionResult(findings=[
        ExtractedField(entity_name="Clinic", field_name="price", field_value="₹900", value_status="found", supporting_excerpt="Clinic consultation price: ₹500.")
    ])
    with pytest.raises(ValueError, match="not present in its supporting excerpt"):
        research_workflow._validate_extraction(result, ["price"], "Clinic consultation price: ₹500.")


def test_request_rejects_unknown_properties():
    with pytest.raises(ValidationError):
        ResearchRequest(query="Find clinics", arbitrary_tool="unsafe")


def test_request_validates_reference_urls_and_text_materials():
    request = ResearchRequest(
        query="Summarize the supplied information",
        options={"reference_urls": ["https://example.org/guide"]},
        input_materials=[{"filename": "notes.md", "content": "A short source note."}],
    )
    assert request.options.reference_urls == ["https://example.org/guide"]
    assert request.input_materials[0].filename == "notes.md"
    with pytest.raises(ValidationError, match="HTTP or HTTPS"):
        ResearchRequest(query="Summarize the supplied information", options={"reference_urls": ["file:///private"]})
    with pytest.raises(ValidationError, match="Only plain text and Markdown"):
        ResearchRequest(query="Summarize the supplied information", input_materials=[{"filename": "notes.pdf", "content": "not parsed"}])
    with pytest.raises(ValidationError, match="50 KB"):
        ResearchRequest(query="Summarize the supplied information", input_materials=[{"filename": "large.txt", "content": "x" * 50_001}])


def test_url_canonicalization_drops_tracking_and_rejects_non_http():
    assert normalize_url("HTTPS://Example.Org/path/?utm_source=search&ref=site#top") == "https://example.org/path?ref=site"
    with pytest.raises(ValueError):
        normalize_url("file:///C:/secret.txt")


@pytest.mark.asyncio
async def test_retry_transient_failure_then_success_persists_each_attempt():
    calls, saved, retries, sleeps = 0, [], [], []
    async def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            error = RuntimeError("transient")
            error.status_code = 503
            raise error
        return "ok"
    async def on_start(n): saved.append((n, "RUNNING"))
    async def on_attempt(n, success, classification, safe): saved.append((n, "SUCCEEDED" if success else classification.name))
    async def on_retry(n, classification, delay): retries.append((n, classification.name, delay))
    async def no_sleep(delay): sleeps.append(delay)

    result = await run_with_retries(operation, max_attempts=3, base_seconds=1, max_seconds=8,
        on_start=on_start, on_attempt=on_attempt, on_retry=on_retry, sleep=no_sleep, jitter=lambda low, high: low)
    assert result == "ok"
    assert calls == 2
    assert saved == [(1, "RUNNING"), (1, "temporary_server_error"), (2, "RUNNING"), (2, "SUCCEEDED")]
    assert retries[0][1] == "temporary_server_error"
    assert sleeps == [0.8]


@pytest.mark.asyncio
async def test_retry_exhaustion_and_nonretryable_authentication():
    calls = 0
    async def always_timeout():
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout")
    async def noop(*_args): pass
    with pytest.raises(httpx.ReadTimeout):
        await run_with_retries(always_timeout, max_attempts=3, base_seconds=0, max_seconds=0,
            on_start=noop, on_attempt=noop, on_retry=noop, sleep=noop)
    assert calls == 3

    auth = TavilyError("unauthorized", status_code=401)
    assert classify_error(auth).name == "authorization"
    assert classify_error(auth).retryable is False
    calls = 0
    async def unauthorized():
        nonlocal calls
        calls += 1
        raise auth
    with pytest.raises(TavilyError):
        await run_with_retries(unauthorized, max_attempts=3, base_seconds=0, max_seconds=0,
            on_start=noop, on_attempt=noop, on_retry=noop, sleep=noop)
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_respects_provider_retry_after():
    calls, delays = 0, []
    async def rate_limited():
        nonlocal calls
        calls += 1
        if calls == 1: raise TavilyError("rate limited", status_code=429, retry_after_seconds=7)
        return []
    async def noop(*_args): pass
    async def on_retry(_attempt, _class, delay): delays.append(delay)
    async def no_sleep(_delay): pass
    await run_with_retries(rate_limited, max_attempts=2, base_seconds=0.1, max_seconds=1,
        on_start=noop, on_attempt=noop, on_retry=on_retry, sleep=no_sleep)
    assert delays == [7]


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
        assert body["options"]["num_ctx"] == 4096
        return httpx.Response(200, json={"response": valid_plan().model_dump_json()})

    settings = Settings(_env_file=None, ollama_base_url="http://ollama.test", ollama_model="test-model")
    result = await generate_structured(settings, system="system", prompt="prompt", schema=ResearchPlan, transport=httpx.MockTransport(handler))
    assert isinstance(result, ResearchPlan)
    assert result.actions[0].required_tool == "tavily_search"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, classification", [
    (httpx.ReadTimeout("slow model"), "timeout"),
    (None, "validation"),
    (httpx.ConnectError("offline"), "connection"),
])
async def test_ollama_plan_provider_and_output_failures_are_classified(failure, classification):
    async def handler(_request):
        if failure is not None:
            raise failure
        return httpx.Response(200, json={"response": "not valid json"})

    settings = Settings(_env_file=None, ollama_base_url="http://ollama.test", max_ollama_concurrency=2)
    with pytest.raises(OllamaError) as caught:
        await generate_structured(
            settings, system="system", prompt="prompt", schema=ResearchPlan,
            transport=httpx.MockTransport(handler),
        )
    error_classification = classify_error(caught.value)
    assert error_classification.name == classification
    if classification == "timeout":
        assert error_classification.retryable is False


@pytest.mark.asyncio
async def test_ollama_plan_http_provider_failure_is_reported_without_response_body():
    async def handler(_request):
        return httpx.Response(503, json={"secret": "provider body"})

    settings = Settings(_env_file=None, ollama_base_url="http://ollama.test")
    with pytest.raises(OllamaError) as caught:
        await generate_structured(
            settings, system="system", prompt="prompt", schema=ResearchPlan,
            transport=httpx.MockTransport(handler),
        )
    assert classify_error(caught.value).name == "temporary_server_error"
    assert "provider body" not in str(caught.value)


def test_extraction_chunking_keeps_small_pages_and_selects_relevant_oversized_sections():
    from app.services.extraction import prepare_extraction_chunks
    small = "NASA mission statement is to explore space."
    assert prepare_extraction_chunks(small, query="NASA mission", required_fields=["mission"], chunk_chars=100, max_chunks=3) == [
        (hashlib.sha256(small.encode()).hexdigest()[:20], small)
    ]
    large = "\n\n".join(
        [f"Background section {i}. Routine agency history and unrelated information." for i in range(8)]
        + ["## Mission\nNASA's mission is to explore the unknown in air and space."]
    )
    chunks = prepare_extraction_chunks(large, query="NASA mission", required_fields=["mission"], chunk_chars=100, max_chunks=3)
    assert len(chunks) == 3
    assert all(len(text) <= 100 and text in large for _chunk_id, text in chunks)
    assert any("NASA's mission" in text for _chunk_id, text in chunks)


@pytest.mark.asyncio
async def test_ollama_generation_timeout_is_not_retried():
    calls = 0
    async def operation():
        nonlocal calls
        calls += 1
        raise OllamaError("Ollama request failed", failure_kind="timeout") from httpx.ReadTimeout("timeout")
    async def noop(*_args): pass
    with pytest.raises(OllamaError):
        await run_with_retries(operation, max_attempts=3, base_seconds=0, max_seconds=0,
            on_start=noop, on_attempt=noop, on_retry=noop, sleep=noop)
    assert calls == 1
    classification = classify_error(OllamaError("Ollama request failed", failure_kind="timeout"))
    assert classification.name == "timeout"
    assert classification.retryable is False


@pytest.mark.asyncio
async def test_malformed_ollama_output_is_a_nonretryable_validation_error():
    async def handler(_request):
        return httpx.Response(200, json={"response": '{"findings":[{"field_name":"mission"}]}'})
    settings = Settings(_env_file=None, ollama_base_url="http://ollama.test")
    with pytest.raises(OllamaError) as caught:
        await generate_structured(settings, system="system", prompt="prompt", schema=ExtractionResult,
                                  transport=httpx.MockTransport(handler))
    assert classify_error(caught.value).name == "validation"
    assert classify_error(caught.value).retryable is False
    assert "findings" not in str(caught.value)


def test_extraction_rejects_wrong_entity_or_unrelated_field_evidence():
    result = ExtractionResult(findings=[ExtractedField(
        entity_name="Alpha Care", field_name="price", field_value="₹500", value_status="found",
        supporting_excerpt="Another Clinic consultation price: ₹500.",
    )])
    with pytest.raises(ValueError, match="entity"):
        research_workflow._validate_extraction(
            result, ["price"], "Another Clinic consultation price: ₹500.",
            query="Find Alpha Care consultation price",
        )

    from app.services.extraction import finding_evidence_error
    error = finding_evidence_error(
        entity_name="Alpha Care", field_name="name", field_value="Alpha Care",
        value_status="found", supporting_excerpt="Alpha Care is highly rated.",
        source_text="Alpha Care is highly rated.", query="Find Alpha Care name", required_fields=["name"],
    )
    assert error == "the supporting excerpt did not establish the requested field"


@pytest.mark.asyncio
async def test_extraction_checks_cancellation_between_chunks(monkeypatch):
    from app.services.extraction import extract_fields
    settings = Settings(_env_file=None, ollama_extraction_chunk_chars=1000, ollama_max_extraction_chunks=4)
    calls = 0
    async def fake_generate(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return ExtractionResult(findings=[])
    monkeypatch.setattr(extraction_service, "generate_structured", fake_generate)
    checks = 0
    async def cancellation_check():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise research_workflow.CancellationObserved()
    content = "\n\n".join(f"Alpha Care section {i}: consultation price is listed. " + ("background " * 50) for i in range(5))
    with pytest.raises(research_workflow.CancellationObserved):
        await extract_fields(settings, content=content, required_fields=["price"],
                             query="Find Alpha Care consultation price", check_cancelled=cancellation_check)
    assert calls == 1


@pytest.mark.asyncio
async def test_planner_builds_valid_plan_without_local_model_and_preserves_request_scope():
    request = ResearchRequest(
        query="Find clinic consultation prices",
        options={"required_fields": ["name", "price"], "location": "Bengaluru", "domain": "example.org"},
    )
    plan = await planner_service.create_plan(Settings(_env_file=None), request)
    assert plan.topic == request.query
    assert plan.required_fields == ["name", "price"]
    assert plan.actions[0].expected_result.required_fields == ["name", "price"]
    assert plan.actions[0].query == "Find clinic consultation prices Bengaluru"
    assert plan.actions[0].required_tool == "tavily_search"
    assert len(plan.actions) == 1


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
        self.attempts = {}
        self.events = {}
        self.artifacts = {}
        self.claimed = set()
        self.lock = threading.RLock()

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

    def get_step(self, task_id, action_key):
        return next((row for row in self.steps.values() if row.get("task_id") == task_id and row.get("action_key") == action_key), None)

    def list_steps(self, task_id):
        return sorted([row for row in self.steps.values() if row.get("task_id") == task_id], key=lambda row: row.get("sequence_no", 0))

    def create_attempt(self, row):
        row = dict(row); row.setdefault("id", str(uuid4())); self.attempts[row["id"]] = row; return row

    def update_attempt(self, attempt_id, row):
        self.attempts[attempt_id].update(row)

    def list_attempts(self, task_id):
        return [row for row in self.attempts.values() if row["task_id"] == task_id]

    def append_event(self, task_id, event_type, payload):
        events = self.events.setdefault(task_id, [])
        row = {"id": str(uuid4()), "sequence_no": len(events) + 1, "event_type": event_type, "payload": payload}
        events.append(row); return row

    def list_events(self, task_id, after=0, limit=100):
        return [row for row in self.events.get(task_id, []) if row["sequence_no"] > after][:limit]

    def claim_task(self, task_id, worker_id, *, resume):
        if task_id in self.claimed or self.tasks[task_id].get("cancel_requested"): return False
        self.claimed.add(task_id)
        self.tasks[task_id].update({"status": "RECOVERING" if resume else "PLANNING", "worker_id": worker_id, "lease_expires_at": "2999-01-01T00:00:00+00:00", "cancel_requested": False})
        if resume:
            for step in self.list_steps(task_id):
                if step["status"] == "RUNNING": step["status"] = "RETRYING"
            for attempt in self.list_attempts(task_id):
                if attempt["status"] == "RUNNING": attempt["status"] = "INTERRUPTED"
        return True

    def heartbeat(self, task_id, worker_id):
        return self.tasks.get(task_id, {}).get("worker_id") == worker_id and not self.tasks[task_id].get("cancel_requested")

    def release_task(self, task_id, worker_id):
        if self.tasks.get(task_id, {}).get("worker_id") == worker_id:
            self.tasks[task_id]["worker_id"] = None
            self.claimed.discard(task_id)

    def request_cancel(self, task_id):
        with self.lock:
            task = self.tasks[task_id]
            if task.get("status") == "CANCELLED" or task.get("cancel_requested"):
                return True
            if task.get("status") in {"COMPLETED", "COMPLETED_WITH_LIMITATIONS", "FAILED"}:
                return False
            task["cancel_requested"] = True
            self.append_event(task_id, "task_cancel_requested", {"task_id": task_id, "status": "CANCELLATION_REQUESTED"})
            return True

    def start_attempt(self, task_id, worker_id, action_key, attempt_no, tool_name):
        with self.lock:
            task = self.tasks[task_id]
            if task.get("cancel_requested") or task.get("status") not in {"RUNNING", "RECOVERING", "PLANNING"}:
                return None
            if task.get("worker_id") not in (None, worker_id):
                return None
            step = self.steps[action_key]
            active_attempt = any(row.get("step_id") == step["id"] and row.get("status") == "RUNNING" for row in self.attempts.values())
            if step.get("status") not in {"PENDING", "RUNNING", "RETRYING", "FAILED"} or active_attempt:
                return None
            step.update({"status": "RUNNING", "attempt_count": attempt_no})
            return self.create_attempt({
                "task_id": task_id, "step_id": step["id"], "attempt_no": attempt_no,
                "tool_name": tool_name, "status": "RUNNING",
            })

    def save_findings_if_active(self, task_id, worker_id, rows):
        with self.lock:
            task = self.tasks[task_id]
            if task.get("cancel_requested") or task.get("status") == "CANCELLED":
                return False
            if task.get("worker_id") not in (None, worker_id):
                return False
            self.upsert_findings(rows)
            return True

    def finish_cancellation(self, task_id, worker_id):
        with self.lock:
            task = self.tasks[task_id]
            if task.get("status") == "CANCELLED":
                return True
            if not task.get("cancel_requested") or task.get("worker_id") not in (None, worker_id):
                return False
            for step in self.list_steps(task_id):
                if step.get("status") in {"PENDING", "RUNNING", "RETRYING"}:
                    step.update({"status": "SKIPPED", "completed_at": "cancelled"})
            for attempt in self.list_attempts(task_id):
                if attempt.get("status") == "RUNNING":
                    attempt.update({"status": "INTERRUPTED", "error_classification": "cancelled"})
            task.update({"status": "CANCELLED", "completed_at": "cancelled", "worker_id": None, "lease_expires_at": None})
            return True

    def finalize_pending_cancellations(self):
        count = 0
        for task_id, task in list(self.tasks.items()):
            if task.get("cancel_requested") and task.get("status") != "CANCELLED" and not task.get("worker_id"):
                count += int(self.finish_cancellation(task_id, task.get("worker_id")))
        return count

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
            key = (item["source_id"], item.get("entity_key", ""), item["field_name"])
            prior = self.findings.get(key)
            if prior and prior.get("value_status") == "found" and item.get("value_status") != "found" and item.get("verification_status") != "rejected_semantic":
                result.append(prior)
                continue
            self.findings[key] = item
            result.append(item)
        return result

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def list_sources(self, task_id):
        return [row for row in self.sources.values() if row["task_id"] == task_id]

    def list_tasks(self, limit=20):
        return sorted(self.tasks.values(), key=lambda row: row.get("created_at", ""), reverse=True)[:limit]

    def list_recent_sources(self, limit=50):
        return list(self.sources.values())[:limit]

    def list_findings(self, task_id):
        return [row for row in self.findings.values() if row["task_id"] == task_id]

    def get_artifact(self, artifact_id):
        return self.artifacts.get(artifact_id)

    def save_artifact(self, row):
        self.artifacts[row["id"]] = dict(row)
        return self.artifacts[row["id"]]

    def upload_artifact(self, *_args):
        raise AssertionError("local artifact storage should be used")


@pytest.mark.asyncio
async def test_workflow_searches_reads_extracts_and_streams_mocked_evidence(monkeypatch):
    request = ResearchRequest(query="Find clinic name and consultation price", options={"required_fields": ["name", "price"]})
    settings = Settings(_env_file=None, tavily_api_key="test", firecrawl_api_key="test", max_task_duration_seconds=15, max_provider_attempts=1)
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
            return {"markdown": "Clinic name: Alpha Care. Alpha Care consultation price: ₹500.", "metadata": {"title": "Alpha Care fees"}}

    async def fake_extract(*_args, **_kwargs):
        return ExtractionResult(findings=[
            ExtractedField(entity_name="Alpha Care", field_name="name", field_value="Alpha Care", value_status="found", supporting_excerpt="Clinic name: Alpha Care."),
            ExtractedField(entity_name="Alpha Care", field_name="price", field_value="₹500", value_status="found", supporting_excerpt="Alpha Care consultation price: ₹500."),
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
    assert "mcp_tool_invocation" in event_types
    assert "finding_extracted" in event_types
    assert "rate_limited" in repository.tasks[task_id]["error_summary"]
    assert event_types[-1] == "task_completed"
    plan_transition = next(i for i, event in enumerate(hub.history(task_id))
                           if event["event"] == "task_status" and event["data"].get("status") == "RUNNING")
    search_started = next(i for i, event in enumerate(hub.history(task_id))
                          if event["event"] == "tool_call_started" and event["data"].get("tool") == "mcp_tavily_search")
    assert plan_transition < search_started


@pytest.mark.asyncio
async def test_workflow_generates_selected_deliverable_after_research_finishes(monkeypatch, tmp_path):
    request = ResearchRequest(query="Find project ideas", output_type="report")
    settings = Settings(_env_file=None, artifact_storage_backend="local", artifact_dir=str(tmp_path), max_provider_attempts=1)
    repository = MemoryRepository()
    task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": request.query, "output_type": "report", "status": "QUEUED"})
    hub = ProgressHub()

    async def fake_plan(_settings, _request): return valid_plan()

    class NoResultsSearch:
        def __init__(self, *_args): pass
        async def search(self, *_args, **_kwargs): return []

    monkeypatch.setattr(research_workflow, "create_plan", fake_plan)
    monkeypatch.setattr(research_workflow, "TavilySearch", NoResultsSearch)
    await research_workflow.execute_research(task_id, request, settings, repository, hub)

    assert repository.tasks[task_id]["status"] == "COMPLETED_WITH_LIMITATIONS"
    assert len(repository.artifacts) == 1
    artifact = next(iter(repository.artifacts.values()))
    assert artifact["artifact_type"] == "report"
    assert artifact["status"] == "READY"
    assert __import__("pathlib").Path(artifact["storage_path"]).exists()
    event_types = [event["event"] for event in hub.history(task_id)]
    assert event_types[-2:] == ["artifact_generated", "task_completed"]


@pytest.mark.asyncio
async def test_workflow_persists_user_urls_and_uploads_as_research_sources(monkeypatch):
    request = ResearchRequest(
        query="Find clinic name and consultation price",
        options={"required_fields": ["name", "price"], "reference_urls": ["https://example.org/reference"]},
        input_materials=[{"filename": "clinic-notes.md", "content": "Alpha Clinic consultation price: $40."}],
    )
    settings = Settings(_env_file=None, tavily_api_key="test", firecrawl_api_key="test", max_task_duration_seconds=15, max_provider_attempts=1)
    repository = MemoryRepository()
    task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": request.query, "status": "QUEUED"})

    async def fake_plan(_settings, _request): return valid_plan()

    class NoResultsSearch:
        def __init__(self, *_args): pass
        async def search(self, *_args, **_kwargs): return []

    class FakeScraper:
        def __init__(self, *_args): pass
        async def scrape(self, _url): return {"markdown": "Alpha Clinic offers consultations.", "metadata": {"title": "Clinic page"}}

    async def empty_extraction(*_args, **kwargs):
        return ExtractionResult(findings=[])

    monkeypatch.setattr(research_workflow, "create_plan", fake_plan)
    monkeypatch.setattr(research_workflow, "TavilySearch", NoResultsSearch)
    monkeypatch.setattr(research_workflow, "FirecrawlScraper", FakeScraper)
    monkeypatch.setattr(research_workflow, "extract_fields", empty_extraction)
    monkeypatch.setattr(research_workflow, "is_public_url", lambda _url: asyncio.sleep(0, result=True))

    await research_workflow.execute_research(task_id, request, settings, repository, ProgressHub())

    uploaded = next(row for row in repository.sources.values() if row["source_type"] == "user_upload")
    reference = next(row for row in repository.sources.values() if row["source_type"] == "user_reference")
    assert uploaded["title"] == "clinic-notes.md"
    assert uploaded["page_content"] == "Alpha Clinic consultation price: $40."
    assert uploaded["fetch_status"] == "retrieved"
    assert reference["canonical_url"] == "https://example.org/reference"
    assert reference["fetch_status"] == "retrieved"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, classification", [
    (OllamaError("Ollama request failed: ReadTimeout", failure_kind="provider"), "timeout"),
    (OllamaError("Ollama returned structured output that failed schema validation", failure_kind="validation"), "validation"),
    (OllamaError("Ollama request failed: ConnectError", failure_kind="provider"), "connection"),
])
async def test_planner_failures_persist_terminal_task_step_attempt_and_event(monkeypatch, failure, classification):
    request = ResearchRequest(query="Find clinic consultation price", options={"required_fields": ["price"]})
    settings = Settings(_env_file=None, max_provider_attempts=1, retry_base_delay_seconds=0, retry_max_delay_seconds=0)
    repository = MemoryRepository()
    task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": request.query, "status": "QUEUED"})
    hub = ProgressHub()

    async def failed_plan(_settings, _request):
        if classification == "timeout":
            raise failure from httpx.ReadTimeout("slow model")
        if classification == "connection":
            raise failure from httpx.ConnectError("offline")
        raise failure

    monkeypatch.setattr(research_workflow, "create_plan", failed_plan)
    await research_workflow.execute_research(task_id, request, settings, repository, hub)

    assert repository.tasks[task_id]["status"] == "FAILED"
    assert classification in repository.tasks[task_id]["error_summary"]
    assert repository.steps["create_plan"]["status"] == "FAILED"
    assert repository.steps["create_plan"]["last_error_classification"] == classification
    assert all(attempt["status"] == "FAILED" for attempt in repository.list_attempts(task_id))
    assert hub.history(task_id)[-1]["event"] == "task_error"


@pytest.mark.asyncio
async def test_unexpected_failure_after_attempt_start_closes_running_step(monkeypatch):
    request = ResearchRequest(query="Find clinic consultation price", options={"required_fields": ["price"]})
    settings = Settings(_env_file=None, max_provider_attempts=1)
    repository = MemoryRepository()
    task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": request.query, "status": "QUEUED"})
    hub = ProgressHub()

    async def valid_fake_plan(_settings, _request): return valid_plan()
    monkeypatch.setattr(research_workflow, "create_plan", valid_fake_plan)
    original_append = repository.append_event

    def fail_after_attempt_start(task, event_type, payload):
        if event_type == "tool_call_started":
            raise RuntimeError("simulated event persistence failure")
        return original_append(task, event_type, payload)

    monkeypatch.setattr(repository, "append_event", fail_after_attempt_start)
    await research_workflow.execute_research(task_id, request, settings, repository, hub)

    assert repository.tasks[task_id]["status"] == "FAILED"
    assert repository.steps["create_plan"]["status"] == "FAILED"
    assert all(attempt["status"] != "RUNNING" for attempt in repository.list_attempts(task_id))


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


def test_recent_tasks_sources_and_saved_steps_api_use_repository_data():
    repository = MemoryRepository()
    task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": "Saved research question", "status": "COMPLETED", "created_at": "2026-10-15T00:00:00Z"})
    repository.create_step({"task_id": task_id, "action_key": "plan", "sequence_no": 1, "title": "Plan", "status": "VERIFIED"})
    repository.upsert_source({"task_id": task_id, "url": "https://example.org", "canonical_url": "https://example.org", "source_type": "user_reference", "fetch_status": "retrieved"})
    app.dependency_overrides[research_api.get_repository] = lambda: repository
    try:
        client = TestClient(app)
        assert client.get("/api/research").json()["tasks"][0]["query"] == "Saved research question"
        assert client.get("/api/sources").json()["sources"][0]["url"] == "https://example.org"
        assert client.get(f"/api/research/{task_id}/steps").json()["steps"][0]["action_key"] == "plan"
    finally:
        app.dependency_overrides.clear()


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


def test_supabase_source_listing_retries_remote_protocol_error(monkeypatch):
    import httpx

    monkeypatch.setattr(
        "app.repositories.tasks.get_settings",
        lambda: Settings(_env_file=None, max_provider_attempts=3, retry_base_delay_seconds=0, retry_max_delay_seconds=0),
    )
    monkeypatch.setattr("app.repositories.tasks.time.sleep", lambda _delay: None)

    class Query:
        def __init__(self): self.calls = 0
        def select(self, *_args): return self
        def eq(self, *_args): return self
        def order(self, *_args): return self
        def execute(self):
            self.calls += 1
            if self.calls == 1:
                raise httpx.RemoteProtocolError("simulated connection closed; secret-url-and-token")
            return type("Response", (), {"data": [{"id": "source-1"}]})()

    query = Query()
    class Client:
        def table(self, name):
            assert name == "sources"
            return query

    rows = SupabaseTaskRepository("", "", client=Client()).list_sources(str(uuid4()))
    assert rows == [{"id": "source-1"}]
    assert query.calls == 2


def test_supabase_source_listing_stops_after_bounded_transient_failures(monkeypatch):
    import httpx

    monkeypatch.setattr(
        "app.repositories.tasks.get_settings",
        lambda: Settings(_env_file=None, max_provider_attempts=3, retry_base_delay_seconds=0, retry_max_delay_seconds=0),
    )
    monkeypatch.setattr("app.repositories.tasks.time.sleep", lambda _delay: None)

    class Query:
        calls = 0
        def select(self, *_args): return self
        def eq(self, *_args): return self
        def order(self, *_args): return self
        def execute(self):
            self.calls += 1
            raise httpx.RemoteProtocolError("simulated disconnect with secret credentials")

    query = Query()
    class Client:
        def table(self, _name): return query

    with pytest.raises(PersistenceError, match=r"Supabase source listing failed \(RemoteProtocolError\)") as error:
        SupabaseTaskRepository("", "", client=Client()).list_sources(str(uuid4()))
    assert query.calls == 3
    assert "secret credentials" not in str(error.value)


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
async def test_tavily_permanent_failure_marks_search_step_and_fails_honestly(monkeypatch):
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
        async def search(self, *_args, **_kwargs): raise TavilyError("Tavily search request failed: 401", status_code=401)

    monkeypatch.setattr(research_workflow, "create_plan", fake_plan)
    monkeypatch.setattr(research_workflow, "TavilySearch", BrokenSearch)
    await research_workflow.execute_research(task_id, request, settings, repository, hub)

    assert repository.tasks[task_id]["status"] == "FAILED"
    assert repository.steps["search_clinics"]["status"] == "FAILED"
    assert repository.steps["search_clinics"]["last_error_classification"] == "authorization"
    assert [event["event"] for event in hub.history(task_id)][-1] == "task_error"


def _stage3_mocks(monkeypatch, *, search_counter=None, scrape_counter=None):
    async def fake_plan(_settings, _request): return valid_plan()
    class FakeSearch:
        def __init__(self, *_args): pass
        async def search(self, *_args, **_kwargs):
            if search_counter is not None: search_counter.append("search")
            return [{"url": "https://example.org/prices", "title": "Clinic prices", "score": 0.9}]
    class FakeScraper:
        def __init__(self, *_args): pass
        async def scrape(self, _url):
            if scrape_counter is not None: scrape_counter.append("scrape")
            return {"markdown": "Clinic name: Alpha Care. Alpha Care consultation price: ₹500.", "metadata": {"title": "Alpha Care fees"}}
    async def fake_extract(*_args, **_kwargs):
        return ExtractionResult(findings=[
            ExtractedField(entity_name="Alpha Care", field_name="name", field_value="Alpha Care", value_status="found", supporting_excerpt="Clinic name: Alpha Care."),
            ExtractedField(entity_name="Alpha Care", field_name="price", field_value="₹500", value_status="found", supporting_excerpt="Alpha Care consultation price: ₹500."),
        ])
    monkeypatch.setattr(research_workflow, "create_plan", fake_plan)
    monkeypatch.setattr(research_workflow, "TavilySearch", FakeSearch)
    monkeypatch.setattr(research_workflow, "FirecrawlScraper", FakeScraper)
    monkeypatch.setattr(research_workflow, "extract_fields", fake_extract)
    monkeypatch.setattr(research_workflow, "is_public_url", lambda _url: asyncio.sleep(0, result=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["search_once", "after_source_persist", "lost_finding_response"])
async def test_failure_injection_recovers_and_deduplicates(monkeypatch, fault):
    request = ResearchRequest(query="Find clinic name and consultation price", options={"required_fields": ["name", "price"]})
    settings = Settings(_env_file=None, tavily_api_key="test", firecrawl_api_key="test", failure_injection_enabled=True,
                        app_env="test", retry_base_delay_seconds=0, max_provider_attempts=3)
    repository = MemoryRepository(); task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": request.query, "status": "QUEUED", "config": {"failure_injection": fault}})
    calls = []; _stage3_mocks(monkeypatch, search_counter=calls)
    hub = ProgressHub()

    await research_workflow.execute_research(task_id, request, settings, repository, hub)

    assert repository.tasks[task_id]["status"] == "COMPLETED"
    assert len(repository.sources) == 1
    assert len(repository.findings) == 2
    assert any(event["event_type"] == "retry_started" for event in repository.events[task_id])
    if fault == "search_once":
        assert len(calls) == 1  # Injected failure occurred before the mocked provider call.
    if fault == "after_source_persist":
        assert len(calls) == 2
        assert len([row for row in repository.attempts.values() if row["tool_name"] == "mcp_tavily_search"]) == 2
    if fault == "lost_finding_response":
        assert len([row for row in repository.attempts.values() if row["tool_name"] == "ollama_extract"]) == 2


@pytest.mark.asyncio
async def test_page_timeout_exhaustion_is_recorded_as_failed(monkeypatch):
    request = ResearchRequest(query="Find clinic name and consultation price", options={"required_fields": ["name", "price"]})
    settings = Settings(_env_file=None, tavily_api_key="test", firecrawl_api_key="test", failure_injection_enabled=True,
                        app_env="test", retry_base_delay_seconds=0, max_provider_attempts=3)
    repository = MemoryRepository(); task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": request.query, "status": "QUEUED", "config": {"failure_injection": "page_timeout"}})
    scraper_calls = []; _stage3_mocks(monkeypatch, scrape_counter=scraper_calls)
    await research_workflow.execute_research(task_id, request, settings, repository, ProgressHub())
    read_step = next(row for row in repository.steps.values() if row["action_type"] == "fetch_and_extract")
    attempts = [row for row in repository.attempts.values() if row["step_id"] == read_step["id"]]
    assert repository.tasks[task_id]["status"] == "FAILED"
    assert len(attempts) == 3
    assert all(row["error_classification"] == "timeout" for row in attempts)
    assert scraper_calls == []


@pytest.mark.asyncio
async def test_resume_after_interruption_preserves_verified_work(monkeypatch):
    request = ResearchRequest(query="Find clinic name and consultation price", options={"required_fields": ["name", "price"]})
    settings = Settings(_env_file=None, tavily_api_key="test", firecrawl_api_key="test", failure_injection_enabled=True,
                        app_env="test", retry_base_delay_seconds=0, max_provider_attempts=1)
    repository = MemoryRepository(); task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": request.query, "status": "QUEUED", "config": {"failure_injection": "restart_after_step"}})
    searches = []; _stage3_mocks(monkeypatch, search_counter=searches)
    await research_workflow.execute_research(task_id, request, settings, repository, ProgressHub())
    assert repository.tasks[task_id]["status"] == "RUNNING"
    verified_before = [step["action_key"] for step in repository.list_steps(task_id) if step["status"] == "VERIFIED"]
    assert "search_clinics" in verified_before

    worker_id = str(uuid4())
    assert repository.claim_task(task_id, worker_id, resume=True)
    await research_workflow.execute_research(task_id, request, settings, repository, ProgressHub(), resume=True, worker_id=worker_id)
    assert repository.tasks[task_id]["status"] == "COMPLETED"
    assert searches == ["search"]
    assert len(repository.sources) == 1
    assert len(repository.findings) == 2
    assert [step["action_key"] for step in repository.list_steps(task_id)].count("search_clinics") == 1


@pytest.mark.asyncio
async def test_partial_extraction_resumes_only_unfinished_chunk_without_duplicate_findings(monkeypatch):
    request = ResearchRequest(query="Find clinic name and consultation price", options={"required_fields": ["name", "price"]})
    settings = Settings(_env_file=None, tavily_api_key="test", firecrawl_api_key="test", max_provider_attempts=1,
                        ollama_extraction_chunk_chars=1000, ollama_max_extraction_chunks=3)
    repository = MemoryRepository(); task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": request.query, "status": "QUEUED"})
    searches, scrapes, calls = [], [], {"name": 0, "price": 0}
    page = "Clinic name: Alpha Care. " + ("Background history. " * 35) + "\n\n"
    page += "Alpha Care consultation price: ₹500. " + ("Pricing details. " * 35) + "\n\n"
    page += "Contact the clinic for current appointment availability."

    async def fake_plan(_settings, _request): return valid_plan()
    class FakeSearch:
        def __init__(self, *_args): pass
        async def search(self, *_args, **_kwargs):
            searches.append(1)
            return [{"url": "https://example.org/prices", "title": "Clinic prices"}]
    class FakeScraper:
        def __init__(self, *_args): pass
        async def scrape(self, _url):
            scrapes.append(1)
            return {"markdown": page, "metadata": {"title": "Alpha Care"}}
    async def fake_generate(_settings, *, prompt, **_kwargs):
        section = prompt.split("BEGIN UNTRUSTED SOURCE TEXT\n", 1)[1].split("\nEND UNTRUSTED SOURCE TEXT", 1)[0]
        if "Clinic name:" in section:
            calls["name"] += 1
            return ExtractionResult(findings=[ExtractedField(
                entity_name="Alpha Care", field_name="name", field_value="Alpha Care",
                value_status="found", supporting_excerpt="Clinic name: Alpha Care.")])
        if "consultation price:" in section.casefold():
            calls["price"] += 1
            if calls["price"] == 1:
                raise OllamaError("Ollama request failed", failure_kind="provider") from httpx.ReadTimeout("timeout")
            return ExtractionResult(findings=[ExtractedField(
                entity_name="Alpha Care", field_name="price", field_value="₹500",
                value_status="found", supporting_excerpt="Alpha Care consultation price: ₹500.")])
        return ExtractionResult(findings=[])
    monkeypatch.setattr(research_workflow, "create_plan", fake_plan)
    monkeypatch.setattr(research_workflow, "TavilySearch", FakeSearch)
    monkeypatch.setattr(research_workflow, "FirecrawlScraper", FakeScraper)
    monkeypatch.setattr(research_workflow, "is_public_url", lambda _url: asyncio.sleep(0, result=True))
    monkeypatch.setattr(extraction_service, "generate_structured", fake_generate)

    await research_workflow.execute_research(task_id, request, settings, repository, ProgressHub())
    assert repository.tasks[task_id]["status"] == "COMPLETED_WITH_LIMITATIONS"
    assert len(repository.findings) == 1
    read_step = next(step for step in repository.list_steps(task_id) if step["action_type"] == "fetch_and_extract")
    assert len(read_step["result"]["completed_chunks"]) == 1
    assert read_step["last_error_classification"] == "timeout"

    # Simulate an older persisted claim whose excerpt does not support its value.
    prior_name = next(row for row in repository.findings.values() if row["field_name"] == "name")
    prior_name["supporting_excerpt"] = "Welcome to our clinic."

    worker_id = str(uuid4())
    await research_workflow.execute_research(task_id, request, settings, repository, ProgressHub(), resume=True, worker_id=worker_id)
    assert repository.tasks[task_id]["status"] == "COMPLETED_WITH_LIMITATIONS"
    assert len(repository.findings) == 2
    questionable = next(row for row in repository.findings.values() if row["field_name"] == "name")
    assert questionable["value_status"] == "unverified"
    assert questionable["field_value"] is None
    assert questionable["verification_status"] != "source_supported"
    assert calls == {"name": 1, "price": 2}
    assert searches == [1]
    assert scrapes == [1]


@pytest.mark.asyncio
async def test_task_deadline_closes_active_attempt_and_marks_step_timeout(monkeypatch):
    request = ResearchRequest(query="Find clinic name and consultation price", options={"required_fields": ["name", "price"]})
    settings = Settings(_env_file=None, tavily_api_key="test", firecrawl_api_key="test", max_task_duration_seconds=10,
                        max_provider_attempts=1, ollama_extraction_chunk_chars=1000)
    repository = MemoryRepository(); task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": request.query, "status": "QUEUED"})
    async def fake_plan(_settings, _request): return valid_plan()
    class Search:
        def __init__(self, *_args): pass
        async def search(self, *_args, **_kwargs): return [{"url": "https://example.org/prices", "title": "Clinic"}]
    class Scraper:
        def __init__(self, *_args): pass
        async def scrape(self, _url): return {"markdown": "Clinic name: Alpha Care.", "metadata": {}}
    async def never_finishes(*_args, **_kwargs): await asyncio.sleep(2)
    original_timeout = asyncio.timeout
    monkeypatch.setattr(research_workflow.asyncio, "timeout", lambda _seconds: original_timeout(0.25))
    monkeypatch.setattr(research_workflow, "create_plan", fake_plan)
    monkeypatch.setattr(research_workflow, "TavilySearch", Search)
    monkeypatch.setattr(research_workflow, "FirecrawlScraper", Scraper)
    monkeypatch.setattr(research_workflow, "extract_fields", never_finishes)
    monkeypatch.setattr(research_workflow, "is_public_url", lambda _url: asyncio.sleep(0, result=True))
    await research_workflow.execute_research(task_id, request, settings, repository, ProgressHub())
    assert repository.tasks[task_id]["status"] == "FAILED"
    assert "MAX_TASK_DURATION_SECONDS" in repository.tasks[task_id]["error_summary"]
    running = [row for row in repository.attempts.values() if row.get("status") == "RUNNING"]
    assert running == []
    failed_reads = [step for step in repository.list_steps(task_id) if step.get("action_type") == "fetch_and_extract" and step.get("status") == "FAILED"]
    assert failed_reads and failed_reads[0]["last_error_classification"] == "timeout"


@pytest.mark.asyncio
async def test_cancellation_is_observed_before_any_provider_call(monkeypatch):
    request = ResearchRequest(query="Find clinic name and consultation price", options={"required_fields": ["name", "price"]})
    settings = Settings(_env_file=None)
    repository = MemoryRepository(); task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": request.query, "status": "QUEUED", "cancel_requested": True})
    searches = []; _stage3_mocks(monkeypatch, search_counter=searches)
    await research_workflow.execute_research(task_id, request, settings, repository, ProgressHub())
    assert repository.tasks[task_id]["status"] == "CANCELLED"
    assert searches == []


@pytest.mark.asyncio
async def test_cancellation_while_one_source_runs_prevents_waiting_source_and_finding_writes(monkeypatch):
    request = ResearchRequest(query="Find clinic name and consultation price", options={"required_fields": ["name", "price"]})
    settings = Settings(_env_file=None, tavily_api_key="test", firecrawl_api_key="test", max_research_concurrency=1)
    repository = MemoryRepository(); task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": request.query, "status": "QUEUED"})
    entered, release = asyncio.Event(), asyncio.Event()
    scrape_calls = []

    async def fake_plan(_settings, _request): return valid_plan()
    class Search:
        def __init__(self, *_args): pass
        async def search(self, *_args, **_kwargs):
            return [
                {"url": "https://example.org/one", "title": "One"},
                {"url": "https://example.org/two", "title": "Two"},
            ]
    class Scraper:
        def __init__(self, *_args): pass
        async def scrape(self, url):
            scrape_calls.append(url)
            entered.set()
            await release.wait()
            return {"markdown": "Alpha Care consultation price: ₹500.", "metadata": {"title": "Alpha Care"}}
    async def should_not_extract(*_args, **_kwargs):
        pytest.fail("extraction must stop after cancellation is observed")
    monkeypatch.setattr(research_workflow, "create_plan", fake_plan)
    monkeypatch.setattr(research_workflow, "TavilySearch", Search)
    monkeypatch.setattr(research_workflow, "FirecrawlScraper", Scraper)
    monkeypatch.setattr(research_workflow, "extract_fields", should_not_extract)
    monkeypatch.setattr(research_workflow, "is_public_url", lambda _url: asyncio.sleep(0, result=True))

    worker = asyncio.create_task(research_workflow.execute_research(task_id, request, settings, repository, ProgressHub()))
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert repository.request_cancel(task_id)
    release.set()
    await asyncio.wait_for(worker, timeout=2)

    assert repository.tasks[task_id]["status"] == "CANCELLED"
    assert len(scrape_calls) == 1
    assert repository.findings == {}
    assert all(step["status"] in {"VERIFIED", "SKIPPED"} for step in repository.list_steps(task_id))


def test_memory_repository_cancellation_and_finding_persistence_share_atomic_boundary():
    repository = MemoryRepository(); task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": "test", "status": "RUNNING"})
    repository.request_cancel(task_id)
    result = repository.save_findings_if_active(task_id, str(uuid4()), [{
        "task_id": task_id, "source_id": "source", "entity_key": "entity", "entity_name": "Entity",
        "field_name": "price", "field_value": "500", "value_status": "found",
        "verification_status": "source_supported", "supporting_excerpt": "Entity price: 500",
    }])
    assert result is False
    assert repository.findings == {}


def test_state_transition_guards_reject_regressions_and_unresolved_completion():
    with pytest.raises(ValueError, match="Illegal step status transition"):
        validate_transition("VERIFIED", "PENDING", kind="step")
    with pytest.raises(ValueError, match="Illegal task status transition"):
        validate_transition("COMPLETED", "RUNNING", kind="task")
    with pytest.raises(ValueError, match="unresolved"):
        validate_task_completion("COMPLETED", ["VERIFIED", "RETRYING"])


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
    app.dependency_overrides[research_api.require_research_providers] = lambda: settings
    app.dependency_overrides[research_api.get_repository] = lambda: repository

    async def ready(_settings):
        return {"connected": True, "model_available": True}

    async def fake_background(task_id, _body, _settings, _repository, hub, **_kwargs):
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
    assert repository.list_events(payload["task_id"])[0]["event_type"] == "task_status"


def test_resume_rejects_active_concurrent_worker(monkeypatch):
    settings = Settings(_env_file=None)
    repository = MemoryRepository()
    task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": "Find clinic consultation prices", "status": "FAILED", "config": {"required_fields": ["price"]}})
    repository.create_step({"task_id": task_id, "action_key": "search", "sequence_no": 1, "status": "FAILED", "last_error_classification": "timeout", "attempt_count": 3})
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[research_api.require_research_providers] = lambda: settings
    app.dependency_overrides[research_api.get_repository] = lambda: repository

    async def fake_background(*_args, **_kwargs): pass
    monkeypatch.setattr(research_api, "execute_research", fake_background)
    try:
        client = TestClient(app)
        first = client.post(f"/api/research/{task_id}/resume")
        second = client.post(f"/api/research/{task_id}/resume")
    finally:
        app.dependency_overrides.clear()
    assert first.status_code == 202
    assert first.json()["status"] == "RECOVERING"
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "task_already_running"


def test_resume_reports_outdated_supabase_claim_function(monkeypatch):
    settings = Settings(_env_file=None)
    repository = MemoryRepository()
    task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": "Find clinic consultation prices", "status": "RUNNING", "plan": {"topic": "Clinic prices"}})
    repository.claimed.add(task_id)  # Simulate a stale RPC predicate with no live lease.
    app.dependency_overrides[research_api.require_research_providers] = lambda: settings
    app.dependency_overrides[research_api.get_repository] = lambda: repository
    try:
        response = TestClient(app).post(f"/api/research/{task_id}/resume")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "task_claim_rejected"
    assert "20261017_stage3_resume_claim.sql" in response.json()["detail"]["message"]


def test_cancel_persists_request_and_rejects_terminal_tasks():
    repository = MemoryRepository()
    task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": "Find clinic consultation prices", "status": "RUNNING"})
    app.dependency_overrides[research_api.get_repository] = lambda: repository
    try:
        client = TestClient(app)
        response = client.post(f"/api/research/{task_id}/cancel")
        repository.finish_cancellation(task_id, str(uuid4()))
        repeated = client.post(f"/api/research/{task_id}/cancel")
        terminal_id = str(uuid4())
        repository.create_task({"id": terminal_id, "query": "Find clinic consultation prices", "status": "COMPLETED"})
        terminal = client.post(f"/api/research/{terminal_id}/cancel")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert repository.tasks[task_id]["cancel_requested"] is True
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "CANCELLED"
    assert terminal.status_code == 409


def test_failure_injection_is_disabled_without_explicit_dev_setting():
    settings = Settings(_env_file=None)
    repository = MemoryRepository()
    app.dependency_overrides[research_api.require_research_providers] = lambda: settings
    app.dependency_overrides[research_api.get_repository] = lambda: repository
    try:
        response = TestClient(app).post("/api/research", json={"query": "Find clinic prices", "failure_injection": "search_once"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 403
    assert not repository.tasks


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
    api_memory_repository.create_task({"id": task_id, "query": "Find clinic pricing", "status": "COMPLETED", "summary": "Finished"})
    api_memory_repository.append_event(task_id, "task_status", {"task_id": task_id, "status": "RUNNING"})
    api_memory_repository.append_event(task_id, "task_completed", {"task_id": task_id, "status": "COMPLETED", "summary": "Finished"})

    response = TestClient(app).get(f"/api/research/{task_id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: task_status" in response.text
    assert "event: task_completed" in response.text

    replay = TestClient(app).get(f"/api/research/{task_id}/events", headers={"Last-Event-ID": "1"})
    assert "event: task_completed" in replay.text
    assert "event: task_status" not in replay.text


def test_single_user_api_rejects_non_loopback_clients():
    response = TestClient(app, client=("203.0.113.9", 49152)).get("/api/health")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "local_access_only"


def test_repository_does_not_treat_false_rpc_result_as_success():
    class Response:
        data = [False]

    class Rpc:
        def execute(self): return Response()

    class Client:
        def rpc(self, *_args): return Rpc()

    repo = SupabaseTaskRepository("", "", client=Client())
    assert repo.claim_task(str(uuid4()), str(uuid4()), resume=True) is False


def test_resume_claim_matches_rpc_contract_and_serializes_two_workers():
    class RpcResponse:
        def __init__(self, data): self.data = data

    class AtomicClaimClient:
        def __init__(self):
            self.lock = __import__("threading").Lock()
            self.claimed_worker = None
            self.calls = []
        def rpc(self, name, args):
            self.calls.append((name, args))
            client = self
            class Builder:
                def execute(self):
                    with client.lock:
                        # Models the SQL WHERE eligibility and lease predicates
                        # with an atomic row claim under concurrent calls.
                        available = client.claimed_worker is None
                        if available: client.claimed_worker = args["p_worker_id"]
                        return RpcResponse(available)
            return Builder()

    client = AtomicClaimClient()
    repo = SupabaseTaskRepository("", "", client=client)
    task_id = str(uuid4())
    workers = [str(uuid4()), str(uuid4())]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda worker: repo.claim_task(task_id, worker, resume=True), workers))
    assert sorted(results) == [False, True]
    assert all(name == "claim_research_task" for name, _args in client.calls)
    assert all(set(args) == {"p_task_id", "p_worker_id", "p_resume"} for _name, args in client.calls)
    assert all(args["p_task_id"] == task_id and args["p_resume"] is True for _name, args in client.calls)


def test_resume_claim_migration_has_no_lease_bypass_and_allows_interrupted_run():
    from pathlib import Path
    sql = Path(__file__).parents[1].joinpath("supabase/migrations/20261017_stage3_resume_claim.sql").read_text(encoding="utf-8")
    compact = " ".join(sql.lower().split())
    assert "create or replace function public.claim_research_task(p_task_id uuid, p_worker_id uuid, p_resume boolean)" in compact
    assert "returns boolean" in compact
    assert "t.status in ('queued','planning','failed','completed_with_limitations','running','recovering')" in compact
    assert "t.worker_id is null or t.lease_expires_at is null or t.lease_expires_at < now()" in compact
    assert "(t.status in ('running','recovering') and t.plan is not null)" in compact
    assert "returning true into claimed" in compact


def test_permanent_failed_source_step_is_not_resumable(monkeypatch):
    settings = Settings(_env_file=None)
    repository = MemoryRepository()
    task_id = str(uuid4())
    repository.create_task({"id": task_id, "query": "Find clinic consultation prices", "status": "COMPLETED_WITH_LIMITATIONS", "plan": {"topic": "Clinic prices"}, "config": {}})
    source = repository.upsert_source({"task_id": task_id, "url": "https://example.org/prices", "canonical_url": "https://example.org/prices", "fetch_status": "fetch_failed"})
    key = "read_" + hashlib.sha256(source["canonical_url"].encode()).hexdigest()[:24]
    repository.create_step({"task_id": task_id, "action_key": key, "sequence_no": 2, "action_type": "fetch_and_extract", "status": "FAILED", "last_error_classification": "authorization"})
    app.dependency_overrides[research_api.require_research_providers] = lambda: settings
    app.dependency_overrides[research_api.get_repository] = lambda: repository
    try:
        response = TestClient(app).post(f"/api/research/{task_id}/resume")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "task_not_resumable"

