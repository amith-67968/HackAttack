import asyncio
import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from app.core.config import Settings
from app.schemas.evidence import ExtractionResult
from app.schemas.plan import ResearchPlan
from app.schemas.research import ResearchRequest
from app.services.extraction import extract_fields
from app.services.ollama import OllamaError
from app.services.planner import create_plan
from app.services.progress import ProgressHub
from app.services.url_safety import is_public_url, normalize_url
from app.tools.firecrawl import FirecrawlError, FirecrawlScraper
from app.tools.tavily import TavilyError, TavilySearch


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _call(repository: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    return getattr(repository, method)(*args, **kwargs)


async def _persist(repository: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(_call, repository, method, *args, **kwargs)


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "unknown"


def _validate_extraction(result: ExtractionResult, required_fields: list[str], content: str) -> None:
    expected = {_key(field) for field in required_fields}
    found: set[str] = set()
    for item in result.findings:
        name = _key(item.field_name)
        if name not in expected:
            raise ValueError(f"Extractor returned an unrequested field: {item.field_name}")
        if name in found:
            raise ValueError(f"Extractor returned a duplicate field: {item.field_name}")
        found.add(name)
        if item.value_status == "found" and item.supporting_excerpt not in content:
            raise ValueError("A supporting excerpt was not present verbatim in the source")
        if item.value_status == "found" and item.field_value and item.field_value.casefold() not in item.supporting_excerpt.casefold():
            raise ValueError("The extracted value was not present in its supporting excerpt")
    missing = expected - found
    if missing:
        raise ValueError(f"Extractor omitted required fields: {', '.join(sorted(missing))}")


async def execute_research(
    task_id: str,
    request: ResearchRequest,
    settings: Settings,
    repository: Any,
    hub: ProgressHub,
) -> None:
    """Execute the bounded Stage 2 workflow; no retry or recovery loop is used here."""
    warnings: list[str] = []
    candidates: dict[str, dict[str, Any]] = {}
    sources_read = 0
    findings_saved = 0
    began = asyncio.get_running_loop().time()

    async def emit(kind: str, data: dict[str, Any]) -> None:
        await hub.publish(task_id, kind, {"task_id": task_id, **data})

    try:
        async with asyncio.timeout(settings.max_task_duration_seconds):
            await _persist(repository, "update_task", task_id, {"status": "PLANNING", "started_at": utc_now(), "updated_at": utc_now()})
            await emit("task_status", {"status": "PLANNING", "message": "Creating a structured plan with the configured Ollama model."})
            plan = await create_plan(settings, request)
            requested = {_key(x) for x in request.options.required_fields}
            planned = {_key(x) for x in plan.required_fields}
            if not requested.issubset(planned):
                raise ValueError("The generated plan did not preserve every requested field")
            plan_json = plan.model_dump(mode="json")
            await _persist(repository, "update_task", task_id, {
                "normalized_topic": plan.topic,
                "plan": plan_json,
                "status": "RUNNING",
                "updated_at": utc_now(),
            })
            await emit("task_status", {"status": "RUNNING", "message": plan.summary, "plan": plan_json})

            for sequence, action in enumerate(plan.actions, start=1):
                await _persist(repository, "create_step", {
                    "task_id": task_id,
                    "action_key": action.action_id,
                    "sequence_no": sequence,
                    "action_type": action.action_type,
                    "title": action.goal,
                    "input": {"query": action.query},
                    "expected_result": action.expected_result.model_dump(mode="json"),
                    "verification_rule": {"rule": action.verification_rule},
                    "status": "PENDING",
                    "attempt_count": 0,
                })

            search = TavilySearch(settings.tavily_api_key, settings.provider_timeout_seconds)
            for sequence, action in enumerate(plan.actions, start=1):
                await _persist(repository, "update_step", task_id, action.action_id, {
                    "status": "RUNNING", "attempt_count": 1, "started_at": utc_now(), "updated_at": utc_now(),
                })
                await emit("step_started", {"step_id": action.action_id, "action_type": "web_search", "title": action.goal})
                await emit("tool_call_started", {"step_id": action.action_id, "tool": "tavily_search", "query": action.query})
                try:
                    results = await search.search(action.query, domain=request.options.domain, max_results=min(8, settings.max_pages_per_task))
                except TavilyError as exc:
                    message = str(exc)
                    warnings.append(message)
                    await _persist(repository, "update_step", task_id, action.action_id, {
                        "status": "FAILED", "last_error": message, "completed_at": utc_now(), "updated_at": utc_now(),
                    })
                    await emit("step_completed", {"step_id": action.action_id, "status": "FAILED", "message": message})
                    continue
                discovered: list[str] = []
                for result in results:
                    raw_url = result.get("url")
                    if not isinstance(raw_url, str):
                        continue
                    try:
                        canonical = normalize_url(raw_url)
                    except ValueError:
                        continue
                    if canonical in candidates:
                        continue
                    if len(candidates) >= settings.max_pages_per_task:
                        break
                    parsed = urlsplit(canonical)
                    source = await _persist(repository, "upsert_source", {
                        "task_id": task_id,
                        "url": raw_url,
                        "canonical_url": canonical,
                        "title": str(result.get("title") or "")[:500] or None,
                        "domain": parsed.hostname or "",
                        "source_type": "web_search",
                        "retrieved_at": None,
                        "content_excerpt": None,
                        "quality_notes": f"Discovered by Tavily query: {action.query[:300]}",
                        "fetch_status": "discovered",
                    })
                    source_id = str(source["id"])
                    candidates[canonical] = {"id": source_id, "url": raw_url, "canonical_url": canonical, "title": source.get("title") or result.get("title"), "query": action.query}
                    discovered.append(source_id)
                    await emit("source_discovered", {"source_id": source_id, "url": raw_url, "title": source.get("title") or result.get("title"), "query": action.query})
                await _persist(repository, "update_step", task_id, action.action_id, {
                    "status": "SUCCEEDED", "result": {"candidate_source_ids": discovered, "result_count": len(discovered)},
                    "completed_at": utc_now(), "updated_at": utc_now(),
                })
                await emit("step_completed", {"step_id": action.action_id, "status": "SUCCEEDED", "sources_discovered": len(discovered)})

            scraper = FirecrawlScraper(settings.firecrawl_api_key, settings.provider_timeout_seconds)
            semaphore = asyncio.Semaphore(max(1, settings.max_research_concurrency))

            async def process_source(sequence: int, source: dict[str, Any]) -> None:
                nonlocal sources_read, findings_saved
                key = "read_" + hashlib.sha256(source["canonical_url"].encode()).hexdigest()[:24]
                async with semaphore:
                    await _persist(repository, "create_step", {
                        "task_id": task_id,
                        "action_key": key,
                        "sequence_no": len(plan.actions) + sequence,
                        "action_type": "fetch_and_extract",
                        "title": f"Read and extract: {source['title'] or source['canonical_url']}",
                        "input": {"source_id": source["id"], "url": source["url"]},
                        "expected_result": {"description": "Required fields supported by source excerpts", "required_fields": plan.required_fields},
                        "verification_rule": {"rule": "Found value excerpts must exactly match page text"},
                        "status": "PENDING", "attempt_count": 0,
                    })
                    await _persist(repository, "update_step", task_id, key, {
                        "status": "RUNNING", "attempt_count": 1, "started_at": utc_now(), "updated_at": utc_now(),
                    })
                    await emit("step_started", {"step_id": key, "action_type": "fetch_and_extract", "source_id": source["id"], "title": source["title"] or source["canonical_url"]})
                    await emit("tool_call_started", {"step_id": key, "tool": "firecrawl_scrape", "source_id": source["id"]})
                    if not await is_public_url(source["canonical_url"]):
                        message = "URL was rejected because it was not a verified public HTTP(S) destination"
                        warnings.append(message)
                        await _persist(repository, "update_source", source["id"], {"fetch_status": "blocked_url", "quality_notes": message})
                        await _persist(repository, "update_step", task_id, key, {"status": "SKIPPED", "last_error": message, "completed_at": utc_now(), "updated_at": utc_now()})
                        await emit("step_completed", {"step_id": key, "status": "SKIPPED", "message": message})
                        return
                    try:
                        page = await scraper.scrape(source["canonical_url"])
                    except FirecrawlError as exc:
                        message = str(exc)
                        warnings.append(message)
                        await _persist(repository, "update_source", source["id"], {"fetch_status": "fetch_failed", "quality_notes": message})
                        await _persist(repository, "update_step", task_id, key, {"status": "FAILED", "last_error": message, "completed_at": utc_now(), "updated_at": utc_now()})
                        await emit("step_completed", {"step_id": key, "status": "FAILED", "message": message})
                        return
                    content = page["markdown"][:settings.max_page_content_chars]
                    metadata = page.get("metadata") or {}
                    title = metadata.get("title") or source["title"]
                    await _persist(repository, "update_source", source["id"], {
                        "title": str(title)[:500] if title else None,
                        "retrieved_at": utc_now(),
                        "content_excerpt": content[:1200],
                        "fetch_status": "retrieved",
                    })
                    sources_read += 1
                    try:
                        extraction = await extract_fields(settings, content=content, required_fields=plan.required_fields, query=request.query)
                        _validate_extraction(extraction, plan.required_fields, content)
                    except (ValueError, OllamaError) as exc:
                        message = f"Extraction failed for {source['canonical_url']}: {exc}"
                        warnings.append(message)
                        await _persist(repository, "update_source", source["id"], {"fetch_status": "extraction_failed", "quality_notes": message})
                        await _persist(repository, "update_step", task_id, key, {"status": "FAILED", "last_error": str(exc), "completed_at": utc_now(), "updated_at": utc_now()})
                        await emit("step_completed", {"step_id": key, "status": "FAILED", "message": "Page was read, but structured extraction could not be validated."})
                        return
                    findings = []
                    for item in extraction.findings:
                        found = item.value_status == "found"
                        findings.append({
                            "task_id": task_id,
                            "entity_key": _key(item.entity_name),
                            "entity_name": item.entity_name,
                            "field_name": item.field_name,
                            "field_value": item.field_value,
                            "value_status": item.value_status,
                            "source_id": source["id"],
                            "supporting_excerpt": item.supporting_excerpt,
                            "verification_status": "source_supported" if found else "missing_or_uncertain",
                            "confidence_notes": "Source-backed extraction only; independent cross-checking is not part of Stage 2.",
                        })
                    await _persist(repository, "upsert_findings", findings)
                    findings_saved += len(findings)
                    await emit("finding_extracted", {"step_id": key, "source_id": source["id"], "count": len(findings), "entity_name": extraction.findings[0].entity_name if extraction.findings else None})
                    await _persist(repository, "update_step", task_id, key, {
                        "status": "SUCCEEDED", "result": {"finding_count": len(findings), "source_id": source["id"]},
                        "completed_at": utc_now(), "updated_at": utc_now(),
                    })
                    await emit("step_completed", {"step_id": key, "status": "SUCCEEDED", "findings_saved": len(findings)})

            await asyncio.gather(*(process_source(index, source) for index, source in enumerate(candidates.values(), start=1)))
            elapsed = int(asyncio.get_running_loop().time() - began)
            summary = f"Read {sources_read} source(s) and saved {findings_saved} source-backed field(s) in {elapsed}s. "
            summary += "Extracted values have not been independently cross-checked."
            if not candidates:
                warnings.append("Search returned no usable public web pages.")
            if warnings:
                status = "COMPLETED_WITH_LIMITATIONS"
                summary += f" {len(warnings)} issue(s) limited coverage."
            else:
                status = "COMPLETED"
            await _persist(repository, "update_task", task_id, {
                "status": status,
                "summary": summary,
                "error_summary": "; ".join(warnings)[:4000] or None,
                "completed_at": utc_now(),
                "updated_at": utc_now(),
            })
            await emit("task_completed", {"status": status, "summary": summary, "sources_read": sources_read, "findings_saved": findings_saved, "limitations": warnings})
    except asyncio.TimeoutError:
        message = f"Task exceeded MAX_TASK_DURATION_SECONDS ({settings.max_task_duration_seconds})"
        await _fail_task(task_id, message, repository, hub)
    except Exception as exc:
        message = str(exc)[:1000] or exc.__class__.__name__
        await _fail_task(task_id, message, repository, hub)


async def _fail_task(task_id: str, message: str, repository: Any, hub: ProgressHub) -> None:
    try:
        await _persist(repository, "update_task", task_id, {
            "status": "FAILED", "error_summary": message, "completed_at": utc_now(), "updated_at": utc_now(),
        })
    except Exception:
        pass
    await hub.publish(task_id, "task_error", {"task_id": task_id, "status": "FAILED", "message": message})
