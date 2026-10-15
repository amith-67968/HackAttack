import asyncio
import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.core.config import Settings
from app.schemas.evidence import ExtractionResult
from app.schemas.plan import ResearchPlan
from app.schemas.research import ResearchRequest
from app.services.extraction import extract_fields, finding_evidence_error
from app.services.ollama import OllamaError
from app.services.mcp_search import discover_and_search
from app.services.planner import create_plan
from app.services.progress import ProgressHub
from app.services.retry import ErrorClassification, classify_error, run_with_retries, safe_error_text
from app.services.url_safety import is_public_url, normalize_url
from app.tools.firecrawl import FirecrawlError, FirecrawlScraper
from app.tools.tavily import TavilyError, TavilySearch


class WorkerInterrupted(Exception):
    """Development-only simulated process loss; durable state is left resumable."""


class CancellationObserved(asyncio.CancelledError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _call(repository: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    return getattr(repository, method)(*args, **kwargs)


async def _persist(repository: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(_call, repository, method, *args, **kwargs)


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "unknown"


def _validate_extraction(result: ExtractionResult, required_fields: list[str], content: str, *, query: str = "", require_all: bool = True) -> None:
    expected = {_key(field) for field in required_fields}
    found: set[tuple[str, str]] = set()
    for item in result.findings:
        name = _key(item.field_name)
        if name not in expected:
            raise ValueError(f"Extractor returned an unrequested field: {item.field_name}")
        identity = (_key(item.entity_name), name)
        if identity in found:
            raise ValueError(f"Extractor returned a duplicate field: {item.field_name}")
        found.add(identity)
        error = finding_evidence_error(
            entity_name=item.entity_name, field_name=item.field_name,
            field_value=item.field_value, value_status=item.value_status,
            supporting_excerpt=item.supporting_excerpt, source_text=content,
            query=query, required_fields=required_fields,
        )
        if error:
            raise ValueError(error)
    missing = expected - {field_name for _entity_name, field_name in found}
    if require_all and missing:
        raise ValueError(f"Extractor omitted required fields: {', '.join(sorted(missing))}")


def _done(step: dict[str, Any] | None) -> bool:
    return bool(step and step.get("status") in {"SUCCEEDED", "VERIFIED", "SKIPPED"})


async def execute_research(
    task_id: str,
    request: ResearchRequest,
    settings: Settings,
    repository: Any,
    hub: ProgressHub,
    *,
    resume: bool = False,
    worker_id: str | None = None,
) -> None:
    """Run unfinished steps only, preserving all completed and idempotently saved work."""
    worker_id = worker_id or str(uuid4())
    warnings: list[str] = []
    sources_read = 0
    findings_saved = 0
    used_injections: set[str] = set()
    cancellation_observed = asyncio.Event()
    try:
        task = await _persist(repository, "get_task", task_id) or {}
        config = task.get("config") or {}
        failure_point = config.get("failure_injection") if settings.failure_injection_enabled and settings.app_env in {"development", "test"} else None

        async def emit(kind: str, data: dict[str, Any]) -> None:
            enriched = {"task_id": task_id, **data}
            # Persist first so a disconnect or process restart cannot lose this event.
            persisted = await _persist(repository, "append_event", task_id, kind, enriched)
            event_id = int(persisted.get("sequence_no", 0))
            await hub.publish(task_id, kind, enriched, event_id=event_id or None)

        async def emit_safely(kind: str, data: dict[str, Any]) -> None:
            try:
                await emit(kind, data)
            except Exception:
                # A failure while reporting must not replace the original execution error.
                pass

        async def check_cancelled() -> None:
            if cancellation_observed.is_set():
                raise CancellationObserved()
            current = await _persist(repository, "get_task", task_id)
            if current and (current.get("cancel_requested") or current.get("status") == "CANCELLED"):
                cancellation_observed.set()
                raise CancellationObserved()
            if current and current.get("worker_id") == worker_id and hasattr(repository, "heartbeat") and not await _persist(repository, "heartbeat", task_id, worker_id):
                cancellation_observed.set()
                raise CancellationObserved()

        async def get_step(key: str) -> dict[str, Any] | None:
            if hasattr(repository, "get_step"):
                return await _persist(repository, "get_step", task_id, key)
            return next((step for step in await _persist(repository, "list_steps", task_id) if step.get("action_key") == key), None)

        async def ensure_step(row: dict[str, Any]) -> dict[str, Any]:
            existing = await get_step(row["action_key"])
            if existing:
                return existing
            return await _persist(repository, "create_step", row)

        async def attempt_action(step: dict[str, Any], tool: str, operation: Any) -> Any:
            step_id = str(step.get("id") or step["action_key"])
            attempt_offset = int(step.get("attempt_count") or 0)
            active_attempt: dict[str, Any] = {}

            async def on_start(attempt_no: int) -> None:
                await check_cancelled()
                absolute_attempt = attempt_offset + attempt_no
                if hasattr(repository, "start_attempt"):
                    attempt = await _persist(
                        repository, "start_attempt", task_id, worker_id,
                        step["action_key"], absolute_attempt, tool,
                    )
                    if not attempt:
                        cancellation_observed.set()
                        raise CancellationObserved()
                else:
                    await _persist(repository, "update_step", task_id, step["action_key"], {
                        "status": "RUNNING", "attempt_count": absolute_attempt,
                        "started_at": utc_now(), "updated_at": utc_now(),
                    })
                    attempt = await _persist(repository, "create_attempt", {
                        "task_id": task_id, "step_id": step_id, "attempt_no": absolute_attempt,
                        "tool_name": tool, "status": "RUNNING", "started_at": utc_now(),
                    })
                active_attempt["id"] = attempt.get("id")
                await emit("tool_call_started", {"step_id": step["action_key"], "tool": tool, "attempt": absolute_attempt})
                if tool.startswith("mcp_"):
                    await emit("mcp_tool_invocation", {"step_id": step["action_key"], "tool_name": tool.removeprefix("mcp_"), "protocol": "MCP", "status": "started", "attempt": absolute_attempt})

            async def on_attempt(attempt_no: int, succeeded: bool, classification: ErrorClassification | None, safe_error: str | None) -> None:
                attempt_id = active_attempt.get("id")
                if attempt_id:
                    await _persist(repository, "update_attempt", attempt_id, {
                        "status": "SUCCEEDED" if succeeded else "FAILED",
                        "error_classification": classification.name if classification else None,
                        "safe_error": safe_error,
                        "completed_at": utc_now(),
                    })
                if not succeeded and classification:
                    await _persist(repository, "update_step", task_id, step["action_key"], {
                        "status": "RETRYING" if classification.retryable and attempt_no < settings.max_provider_attempts else "FAILED",
                        "last_error_classification": classification.name,
                        "last_error": safe_error,
                        "updated_at": utc_now(),
                    })
                await emit("tool_call_finished", {
                    "step_id": step["action_key"], "tool": tool, "attempt": attempt_offset + attempt_no,
                    "status": "SUCCEEDED" if succeeded else "FAILED",
                    "error_classification": classification.name if classification else None,
                })

            async def on_retry(attempt_no: int, classification: ErrorClassification, delay: float) -> None:
                await emit("retry_started", {
                    "step_id": step["action_key"], "tool": tool,
                    "attempt": attempt_offset + attempt_no, "next_attempt": attempt_offset + attempt_no + 1,
                    "error_classification": classification.name, "delay_seconds": delay,
                })

            async def guarded_operation():
                await check_cancelled()
                result = await operation()
                # Providers cannot always be interrupted once sent; never commit
                # their late result after cancellation has crossed its DB boundary.
                await check_cancelled()
                return result

            return await run_with_retries(
                guarded_operation,
                max_attempts=settings.max_provider_attempts,
                base_seconds=settings.retry_base_delay_seconds,
                max_seconds=settings.retry_max_delay_seconds,
                on_start=on_start,
                on_attempt=on_attempt,
                on_retry=on_retry,
            )

        async def injected_call(tool: str, operation: Any) -> Any:
            if failure_point == "exhaust_retries" and tool in {"tavily_search", "mcp_tavily_search"}:
                raise httpx.ConnectError("Injected transient connection failure")
            if failure_point == "search_once" and tool in {"tavily_search", "mcp_tavily_search"} and "search_once" not in used_injections:
                used_injections.add("search_once")
                raise httpx.ConnectError("Injected transient connection failure")
            if failure_point == "page_timeout" and tool == "firecrawl_scrape":
                raise httpx.ReadTimeout("Injected page retrieval timeout")
            return await operation()

        async with asyncio.timeout(settings.max_task_duration_seconds):
            if resume:
                await emit("recovery_started", {"status": "RECOVERING", "message": "Resuming persisted unfinished steps."})
            else:
                await _persist(repository, "update_task", task_id, {"status": "PLANNING", "started_at": utc_now(), "updated_at": utc_now()})
            await emit("task_status", {"status": "PLANNING" if not resume else "RECOVERING", "message": "Loading or creating the structured research plan."})

            plan = ResearchPlan.model_validate(task["plan"]) if task.get("plan") else None
            if plan is None:
                planning = await ensure_step({
                    "task_id": task_id, "action_key": "create_plan", "sequence_no": 1,
                    "action_type": "local_model_plan", "title": "Create a structured research plan",
                    "input": {"query": request.query}, "expected_result": {"description": "Validated research plan"},
                    "verification_rule": {"rule": "Pydantic plan validation and requested-field coverage"},
                    "status": "PENDING", "attempt_count": 0,
                })
                async def plan_op():
                    if failure_point == "restart_after_step" and planning.get("status") == "VERIFIED":
                        return ResearchPlan.model_validate(task["plan"])
                    return await create_plan(settings, request)
                plan = await attempt_action(planning, "ollama_plan", plan_op)
                requested = {_key(x) for x in request.options.required_fields}
                if not requested.issubset({_key(x) for x in plan.required_fields}):
                    raise ValueError("The generated plan did not preserve every requested field")
                plan_json = plan.model_dump(mode="json")
                await _persist(repository, "update_task", task_id, {"normalized_topic": plan.topic, "plan": plan_json, "status": "RUNNING", "updated_at": utc_now()})
                await _persist(repository, "update_step", task_id, "create_plan", {"status": "VERIFIED", "result": plan_json, "completed_at": utc_now(), "updated_at": utc_now()})
                await emit("verification_result", {"step_id": "create_plan", "status": "VERIFIED"})
            else:
                plan_json = plan.model_dump(mode="json")
                await _persist(repository, "update_task", task_id, {"status": "RUNNING", "updated_at": utc_now()})
            await emit("task_status", {"status": "RUNNING", "message": plan.summary, "plan": plan_json})

            # Stable action IDs make retries and resume idempotent.
            for sequence, action in enumerate(plan.actions, start=2):
                await ensure_step({
                    "task_id": task_id, "action_key": action.action_id, "sequence_no": sequence,
                    "action_type": action.action_type, "title": action.goal,
                    "input": {"query": action.query}, "expected_result": action.expected_result.model_dump(mode="json"),
                    "verification_rule": {"rule": action.verification_rule}, "status": "PENDING", "attempt_count": 0,
                })

            search = TavilySearch(settings.tavily_api_key, settings.provider_timeout_seconds)
            candidates: dict[str, dict[str, Any]] = {}
            for action in plan.actions:
                await check_cancelled()
                step = await get_step(action.action_id)
                if _done(step):
                    continue
                if step and step.get("status") == "FAILED" and step.get("last_error_classification") in {"authorization", "validation"}:
                    warnings.append(step.get("last_error") or "A permanent search error requires correction.")
                    continue

                async def search_and_persist():
                    results = await injected_call("mcp_tavily_search", lambda: discover_and_search(
                        search, action.query, domain=request.options.domain, max_results=min(8, settings.max_pages_per_task),
                    ))
                    await check_cancelled()
                    discovered: list[str] = []
                    for result in results:
                        await check_cancelled()
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
                            "task_id": task_id, "url": raw_url, "canonical_url": canonical,
                            "title": str(result.get("title") or "")[:500] or None,
                            "domain": parsed.hostname or "", "source_type": "web_search", "retrieved_at": None,
                            "content_excerpt": None, "quality_notes": f"Discovered by Tavily query: {action.query[:300]}",
                            "fetch_status": "discovered",
                        })
                        source_id = str(source["id"])
                        candidate = {"id": source_id, "url": raw_url, "canonical_url": canonical,
                                     "title": source.get("title") or result.get("title"), "query": action.query,
                                     "search_metadata": {k: result.get(k) for k in ("score", "published_date") if result.get(k) is not None}}
                        candidates[canonical] = candidate
                        discovered.append(source_id)
                        await emit("source_discovered", {"source_id": source_id, "url": raw_url, "title": candidate["title"], "query": action.query})
                        if failure_point == "after_source_persist" and "after_source_persist" not in used_injections:
                            used_injections.add("after_source_persist")
                            # Drop volatile knowledge to model a lost acknowledgment; resume
                            # must rediscover the row via its durable canonical URL.
                            candidates.pop(canonical, None)
                            raise httpx.ReadTimeout("Injected lost response after source save")
                    return discovered

                try:
                    discovered = await attempt_action(step, "mcp_tavily_search", search_and_persist)
                except CancellationObserved:
                    raise
                except Exception as exc:
                    classification = classify_error(exc)
                    warnings.append(safe_error_text(exc, classification))
                    await emit("step_failed", {"step_id": action.action_id, "status": "FAILED", "message": warnings[-1]})
                    continue
                await _persist(repository, "update_step", task_id, action.action_id, {
                    "status": "VERIFIED", "result": {"candidate_source_ids": discovered, "result_count": len(discovered)},
                    "completed_at": utc_now(), "updated_at": utc_now(),
                })
                await emit("verification_result", {"step_id": action.action_id, "status": "VERIFIED"})
                await emit("step_completed", {"step_id": action.action_id, "status": "VERIFIED", "sources_discovered": len(discovered)})
                if failure_point == "restart_after_step":
                    await emit("worker_interrupted", {"step_id": action.action_id, "message": "Development restart point reached; resume to continue."})
                    raise WorkerInterrupted()

            # Explicit reference URLs are user-directed candidates, still subject
            # to the same public-address validation as discovered URLs.
            for raw_url in request.options.reference_urls:
                if len(candidates) >= settings.max_pages_per_task:
                    warnings.append("The per-task page limit was reached before all reference URLs could be added.")
                    break
                try:
                    canonical = normalize_url(raw_url)
                except ValueError:
                    warnings.append("A reference URL was skipped because it was not a valid public HTTP(S) URL.")
                    continue
                if canonical in candidates:
                    continue
                if not await is_public_url(canonical):
                    warnings.append("A reference URL was skipped because it did not resolve to a public network address.")
                    continue
                parsed = urlsplit(canonical)
                source = await _persist(repository, "upsert_source", {
                    "task_id": task_id, "url": raw_url, "canonical_url": canonical,
                    "title": None, "domain": parsed.hostname or "", "source_type": "user_reference",
                    "retrieved_at": None, "content_excerpt": None,
                    "quality_notes": "Reference URL supplied by the user.", "fetch_status": "discovered",
                })
                candidate = {"id": str(source["id"]), "url": raw_url, "canonical_url": canonical,
                             "title": source.get("title"), "query": "User-provided reference URL"}
                candidates[canonical] = candidate
                await emit("source_discovered", {"source_id": candidate["id"], "url": raw_url,
                                                   "title": candidate["title"], "source_type": "user_reference"})

            # Text/Markdown materials are persisted as source rows so extraction,
            # evidence excerpts and restart recovery use the same durable path.
            for material in request.input_materials:
                if len(candidates) >= settings.max_pages_per_task:
                    warnings.append("The per-task page limit was reached before all uploaded materials could be added.")
                    break
                digest = hashlib.sha256((material.filename + "\0" + material.content).encode("utf-8")).hexdigest()
                canonical = f"upload://{digest}"
                if canonical in candidates:
                    continue
                source = await _persist(repository, "upsert_source", {
                    "task_id": task_id, "url": canonical, "canonical_url": canonical,
                    "title": material.filename, "domain": "user-upload", "source_type": "user_upload",
                    "retrieved_at": utc_now(), "content_excerpt": material.content[:1200],
                    "page_content": material.content, "quality_notes": "Text supplied by the user; not independently verified.",
                    "fetch_status": "retrieved",
                })
                candidate = {"id": str(source["id"]), "url": canonical, "canonical_url": canonical,
                             "title": material.filename, "query": "User-uploaded text material"}
                candidates[canonical] = candidate
                await emit("source_discovered", {"source_id": candidate["id"], "url": None,
                                                   "title": material.filename, "source_type": "user_upload"})

            # Existing sources are reconciled from Supabase, including a save whose
            # response was lost before an event or step result could be recorded.
            for source in await _persist(repository, "list_sources", task_id):
                canonical = source.get("canonical_url") or normalize_url(source["url"])
                candidates.setdefault(canonical, {
                    "id": str(source["id"]), "url": source["url"], "canonical_url": canonical,
                    "title": source.get("title"), "query": "Previously discovered source",
                })

            scraper = FirecrawlScraper(settings.firecrawl_api_key, settings.provider_timeout_seconds)
            semaphore = asyncio.Semaphore(max(1, settings.max_research_concurrency))

            async def process_source(sequence: int, source: dict[str, Any]) -> None:
                nonlocal sources_read, findings_saved
                await check_cancelled()
                key = "read_" + hashlib.sha256(source["canonical_url"].encode()).hexdigest()[:24]
                step = await ensure_step({
                    "task_id": task_id, "action_key": key, "sequence_no": len(plan.actions) + sequence + 1,
                    "action_type": "fetch_and_extract", "title": f"Read and extract: {source['title'] or source['canonical_url']}",
                    "input": {"source_id": source["id"], "url": source["url"]},
                    "expected_result": {"description": "Required fields supported by source excerpts", "required_fields": plan.required_fields},
                    "verification_rule": {"rule": "Found value excerpts must exactly match page text"},
                    "status": "PENDING", "attempt_count": 0,
                })
                if _done(step):
                    return
                if step and step.get("status") == "FAILED" and step.get("last_error_classification") in {"authorization", "validation"}:
                    warnings.append(step.get("last_error") or "A permanent retrieval or extraction error requires correction.")
                    return
                source_state = next((item for item in await _persist(repository, "list_sources", task_id) if item["id"] == source["id"]), source)
                async with semaphore:
                    # Tasks can wait here while cancellation is requested.
                    await check_cancelled()
                    await emit("step_started", {"step_id": key, "action_type": "fetch_and_extract", "source_id": source["id"], "title": source["title"] or source["canonical_url"]})
                    is_user_upload = source.get("source_type") == "user_upload"
                    if not is_user_upload and not await is_public_url(source["canonical_url"]):
                        message = "URL was rejected because it was not a verified public HTTP(S) destination"
                        warnings.append(message)
                        await _persist(repository, "update_source", source["id"], {"fetch_status": "blocked_url", "quality_notes": message})
                        await _persist(repository, "update_step", task_id, key, {"status": "SKIPPED", "last_error": message, "completed_at": utc_now(), "updated_at": utc_now()})
                        await emit("step_completed", {"step_id": key, "status": "SKIPPED", "message": message})
                        return

                    content = None
                    if source_state.get("page_content"):
                        # Page retrieval was durably acknowledged before a crash; reuse it.
                        content = source_state["page_content"]
                    if content is None and not is_user_upload:
                        async def retrieve():
                            return await injected_call("firecrawl_scrape", lambda: scraper.scrape(source["canonical_url"]))
                        try:
                            page = await attempt_action(step, "firecrawl_scrape", retrieve)
                        except CancellationObserved:
                            raise
                        except Exception as exc:
                            classification = classify_error(exc)
                            message = safe_error_text(exc, classification)
                            warnings.append(message)
                            await _persist(repository, "update_source", source["id"], {"fetch_status": "fetch_failed", "quality_notes": message})
                            await _persist(repository, "update_step", task_id, key, {
                                "status": "FAILED", "last_error": message,
                                "last_error_classification": classification.name,
                                "completed_at": utc_now(), "updated_at": utc_now(),
                            })
                            await emit("step_failed", {"step_id": key, "status": "FAILED", "message": message})
                            return
                        content = page["markdown"][:settings.max_page_content_chars]
                        await check_cancelled()
                        metadata = page.get("metadata") or {}
                        title = metadata.get("title") or source["title"]
                        await _persist(repository, "update_source", source["id"], {
                            "title": str(title)[:500] if title else None,
                            "retrieved_at": utc_now(), "content_excerpt": content[:1200],
                            "page_content": content,
                            "fetch_status": "retrieved",
                        })
                        sources_read += 1
                    elif is_user_upload and content is not None:
                        sources_read += 1

                    saved_findings = await _persist(repository, "list_findings", task_id)
                    existing_source_findings = [item for item in saved_findings if item.get("source_id") == source["id"]]
                    # Old findings are not trusted just because they were durable.
                    # Keep their values/evidence for audit, but invalidate unsupported
                    # claims and exclude them from the accepted result set.
                    rejected_rows = []
                    accepted_source_findings = []
                    for finding in existing_source_findings:
                        if finding.get("value_status") != "found":
                            continue
                        error = finding_evidence_error(
                            entity_name=str(finding.get("entity_name", "")),
                            field_name=str(finding.get("field_name", "")),
                            field_value=str(finding.get("field_value")) if finding.get("field_value") is not None else None,
                            value_status="found",
                            supporting_excerpt=str(finding.get("supporting_excerpt", "")),
                            source_text=str(source_state.get("page_content") or content or ""),
                            query=request.query, required_fields=plan.required_fields,
                        )
                        if error:
                            prior_note = str(finding.get("confidence_notes") or "").strip()
                            rejected_rows.append({
                                **finding, "field_value": None, "value_status": "unverified",
                                "verification_status": "rejected_semantic",
                                "confidence_notes": (prior_note + " " if prior_note else "")
                                    + f"Previously persisted value excluded during resumed validation: {error}.",
                            })
                        elif finding.get("verification_status") == "source_supported":
                            accepted_source_findings.append(finding)
                    if rejected_rows:
                        await check_cancelled()
                        saved = await _persist(repository, "save_findings_if_active", task_id, worker_id, rejected_rows)
                        if not saved:
                            cancellation_observed.set()
                            raise CancellationObserved()
                        await emit("finding_rejected", {
                            "step_id": key, "source_id": source["id"],
                            "count": len(rejected_rows), "reason": "semantic evidence validation failed",
                        })
                    covered_fields: dict[str, set[str]] = {}
                    for finding in accepted_source_findings:
                        covered_fields.setdefault(finding.get("entity_key", _key(finding.get("entity_name", ""))), set()).add(_key(finding["field_name"]))
                    covered = {_key(field) for field in plan.required_fields}
                    if any(covered.issubset(fields) for fields in covered_fields.values()):
                        # Reconcile a crash after the unique upsert but before step completion.
                        await _persist(repository, "update_step", task_id, key, {
                            "status": "VERIFIED", "result": {"finding_count": len(accepted_source_findings), "source_id": source["id"]},
                            "completed_at": utc_now(), "updated_at": utc_now(),
                        })
                        return

                    async def extract_and_save():
                        current_step = await get_step(key) or step
                        completed_chunks = set((current_step.get("result") or {}).get("completed_chunks", []))

                        async def persist_chunk(chunk_id: str, chunk_result: ExtractionResult) -> None:
                            await check_cancelled()
                            _validate_extraction(chunk_result, plan.required_fields, content or "", query=request.query, require_all=False)
                            durable = await _persist(repository, "list_findings", task_id)
                            source_findings = [item for item in durable if item.get("source_id") == source["id"]]
                            existing_by_key = {
                                (_key(item.get("entity_name", "")), _key(item.get("field_name", ""))): item
                                for item in source_findings
                            }
                            rows = []
                            for item in chunk_result.findings:
                                if item.value_status != "found":
                                    continue
                                identity = (_key(item.entity_name), _key(item.field_name))
                                prior = existing_by_key.get(identity)
                                if prior and prior.get("value_status") == "found":
                                    continue
                                rows.append({
                                    "task_id": task_id, "entity_key": identity[0], "entity_name": item.entity_name,
                                    "field_name": item.field_name, "field_value": item.field_value, "value_status": item.value_status,
                                    "source_id": source["id"], "supporting_excerpt": item.supporting_excerpt,
                                    "verification_status": "source_supported",
                                    "confidence_notes": "Source-backed extraction only; independent cross-checking is not part of this stage.",
                                })
                            if rows:
                                if not await _persist(repository, "save_findings_if_active", task_id, worker_id, rows):
                                    cancellation_observed.set()
                                    raise CancellationObserved()
                            await check_cancelled()
                            completed_chunks.add(chunk_id)
                            prior_result = current_step.get("result") or {}
                            await _persist(repository, "update_step", task_id, key, {
                                "result": {**prior_result, "completed_chunks": sorted(completed_chunks)},
                                "updated_at": utc_now(),
                            })
                            if rows:
                                await emit("finding_extracted", {"step_id": key, "source_id": source["id"], "count": len(rows), "chunk_id": chunk_id})

                        extraction = await extract_fields(
                            settings, content=content or "", required_fields=plan.required_fields,
                            query=request.query, completed_chunks=completed_chunks, on_chunk=persist_chunk,
                            check_cancelled=check_cancelled,
                        )
                        await check_cancelled()
                        _validate_extraction(extraction, plan.required_fields, content or "", query=request.query, require_all=False)
                        # Also merge the completed aggregate. This is idempotent and
                        # reconciles a response whose chunk callback was interrupted.
                        durable = await _persist(repository, "list_findings", task_id)
                        durable_keys = {
                            (_key(item.get("entity_name", "")), _key(item.get("field_name", ""))): item
                            for item in durable if item.get("source_id") == source["id"]
                        }
                        final_rows = []
                        for item in extraction.findings:
                            if item.value_status != "found":
                                continue
                            identity = (_key(item.entity_name), _key(item.field_name))
                            prior = durable_keys.get(identity)
                            if prior and prior.get("value_status") == "found":
                                continue
                            final_rows.append({
                                "task_id": task_id, "entity_key": identity[0], "entity_name": item.entity_name,
                                "field_name": item.field_name, "field_value": item.field_value, "value_status": item.value_status,
                                "source_id": source["id"], "supporting_excerpt": item.supporting_excerpt,
                                "verification_status": "source_supported",
                                "confidence_notes": "Source-backed extraction only; independent cross-checking is not part of this stage.",
                            })
                        if final_rows:
                            if not await _persist(repository, "save_findings_if_active", task_id, worker_id, final_rows):
                                cancellation_observed.set()
                                raise CancellationObserved()
                            await emit("finding_extracted", {"step_id": key, "source_id": source["id"], "count": len(final_rows)})
                        await check_cancelled()
                        durable_after = await _persist(repository, "list_findings", task_id)
                        accepted_after = [
                            row for row in durable_after
                            if row.get("source_id") == source["id"]
                            and row.get("value_status") == "found"
                            and row.get("verification_status") == "source_supported"
                        ]
                        accepted_fields = {_key(row.get("field_name", "")) for row in accepted_after}
                        established_entities = [row.get("entity_name") for row in accepted_after if row.get("entity_name")]
                        if established_entities:
                            missing_entity = established_entities[0]
                        elif _query_entity := next(iter(re.findall(r"\b[A-Z][A-Z0-9]{1,}\b", request.query)), None):
                            missing_entity = _query_entity
                        else:
                            missing_entity = source.get("title") or urlsplit(source["canonical_url"]).hostname or "Source entity"
                        missing_rows = [{
                            "task_id": task_id, "entity_key": _key(str(missing_entity)),
                            "entity_name": str(missing_entity), "field_name": field,
                            "field_value": None, "value_status": "unverified",
                            "source_id": source["id"], "supporting_excerpt": "",
                            "verification_status": "unverified",
                            "confidence_notes": "No excerpt in the retrieved content established this requested field.",
                        } for field in plan.required_fields if _key(field) not in accepted_fields]
                        if missing_rows:
                            if not await _persist(repository, "save_findings_if_active", task_id, worker_id, missing_rows):
                                cancellation_observed.set()
                                raise CancellationObserved()
                        if failure_point == "lost_finding_response" and "lost_finding_response" not in used_injections:
                            used_injections.add("lost_finding_response")
                            raise httpx.ReadTimeout("Injected lost response after finding save")
                        return extraction, completed_chunks

                    # Retrieval and extraction share one durable step but have
                    # separate provider attempts. Refresh its count/status after
                    # Firecrawl so the extraction attempt cannot collide.
                    step = await get_step(key) or step
                    try:
                        extraction, completed_chunks = await attempt_action(step, "ollama_extract", extract_and_save)
                    except CancellationObserved:
                        raise
                    except Exception as exc:
                        if await _cancel_requested(repository, task_id):
                            cancellation_observed.set()
                            raise CancellationObserved() from None
                        classification = classify_error(exc)
                        message = safe_error_text(exc, classification)
                        warnings.append(message)
                        await _persist(repository, "update_source", source["id"], {"fetch_status": "extraction_failed", "quality_notes": message})
                        await _persist(repository, "update_step", task_id, key, {
                            "status": "FAILED", "last_error": message,
                            "last_error_classification": classification.name,
                            "completed_at": utc_now(), "updated_at": utc_now(),
                        })
                        await emit("step_failed", {"step_id": key, "status": "FAILED", "message": "Page was read, but structured extraction could not be validated."})
                        return
                    source_findings = [item for item in await _persist(repository, "list_findings", task_id) if item.get("source_id") == source["id"]]
                    accepted_findings = [item for item in source_findings if item.get("value_status") == "found" and item.get("verification_status") == "source_supported"]
                    findings_saved += len(accepted_findings)
                    found_fields = {
                        _key(item.get("field_name", "")) for item in accepted_findings
                    }
                    missing_fields = [_field for _field in plan.required_fields if _key(_field) not in found_fields]
                    if missing_fields:
                        warnings.append("No source-supported value was extracted for: " + ", ".join(missing_fields))
                    await check_cancelled()
                    await _persist(repository, "update_source", source["id"], {"fetch_status": "retrieved", "quality_notes": None})
                    await _persist(repository, "update_step", task_id, key, {
                        "status": "VERIFIED", "result": {"finding_count": len(source_findings), "source_id": source["id"], "completed_chunks": sorted(completed_chunks)},
                        "completed_at": utc_now(), "updated_at": utc_now(),
                    })
                    await emit("verification_result", {"step_id": key, "status": "VERIFIED"})
                    await emit("step_completed", {"step_id": key, "status": "VERIFIED", "findings_saved": len(source_findings)})

            await asyncio.gather(*(process_source(index, source) for index, source in enumerate(candidates.values(), start=1)))
            if await _cancel_requested(repository, task_id):
                finalized = await _persist(repository, "finish_cancellation", task_id, worker_id)
                if not finalized and hasattr(repository, "finalize_pending_cancellations"):
                    await _persist(repository, "finalize_pending_cancellations")
                await emit("task_status", {"status": "CANCELLED", "message": "Cancellation observed; no additional steps will start."})
                return
            all_steps = await _persist(repository, "list_steps", task_id)
            unresolved = [step for step in all_steps if step.get("status") in {"PENDING", "RUNNING", "RETRYING"}]
            failed = [step for step in all_steps if step.get("status") == "FAILED"]
            if not candidates:
                warnings.append("Search returned no usable public web pages.")
            if failed or unresolved:
                warnings.extend(step.get("last_error") or "A research step remains unresolved." for step in failed)
            status = "FAILED" if not sources_read and not findings_saved and not await _persist(repository, "list_findings", task_id) and failed else (
                "COMPLETED_WITH_LIMITATIONS" if warnings or failed or unresolved else "COMPLETED"
            )
            durable_sources = await _persist(repository, "list_sources", task_id)
            durable_findings = await _persist(repository, "list_findings", task_id)
            accepted_findings = [row for row in durable_findings if row.get("value_status") == "found" and row.get("verification_status") == "source_supported"]
            completed_sources = sum(
                1 for source in durable_sources
                if source.get("fetch_status") == "retrieved" or source.get("page_content")
            )
            summary = f"Read {completed_sources} source(s) and saved {len(accepted_findings)} accepted source-backed field(s). Extracted values have not been independently cross-checked."
            if status == "COMPLETED_WITH_LIMITATIONS":
                summary += f" {len(set(warnings))} issue(s) limited coverage."
            from app.services.transitions import validate_task_completion
            validate_task_completion(status, [step.get("status", "PENDING") for step in all_steps])
            await _persist(repository, "update_task", task_id, {
                "status": status, "summary": summary, "error_summary": "; ".join(dict.fromkeys(warnings))[:4000] or None,
                "completed_at": utc_now(), "updated_at": utc_now(),
            })
            await emit("task_error" if status == "FAILED" else "task_completed", {
                "status": status, "summary": summary, "sources_read": completed_sources,
                "findings_saved": len(accepted_findings), "limitations": list(dict.fromkeys(warnings)),
            })
    except WorkerInterrupted:
        if hasattr(repository, "release_task"):
            try:
                await _persist(repository, "release_task", task_id, worker_id)
            except Exception:
                pass
        # Publish only after releasing the durable lease so a client that reacts
        # immediately to this event can successfully claim the recovery worker.
        await emit_safely("worker_interrupted", {"status": "RUNNING", "message": "Worker stopped at a development recovery point."})
    except CancellationObserved:
        try:
            finalized = await _persist(repository, "finish_cancellation", task_id, worker_id)
            if not finalized and hasattr(repository, "finalize_pending_cancellations"):
                await _persist(repository, "finalize_pending_cancellations")
            await emit_safely("task_status", {"status": "CANCELLED", "message": "Cancellation observed; unfinished steps were stopped."})
        except Exception as exc:
            await _fail_task(task_id, f"Cancellation finalization failed ({exc.__class__.__name__})", repository, hub)
    except asyncio.TimeoutError:
        if await _cancel_requested(repository, task_id):
            try:
                await _persist(repository, "finish_cancellation", task_id, worker_id)
                await emit_safely("task_status", {"status": "CANCELLED", "message": "Cancellation was pending when the worker deadline elapsed."})
            except Exception as exc:
                await _fail_task(task_id, f"Cancellation finalization failed ({exc.__class__.__name__})", repository, hub)
        else:
            await _record_timeout_outcomes(task_id, repository)
            await _fail_task(task_id, f"Task exceeded MAX_TASK_DURATION_SECONDS ({settings.max_task_duration_seconds})", repository, hub)
    except Exception as exc:
        try:
            cancelled = await _cancel_requested(repository, task_id)
        except Exception:
            cancelled = False
        if cancelled:
            try:
                await _persist(repository, "finish_cancellation", task_id, worker_id)
                await emit_safely("task_status", {"status": "CANCELLED", "message": "Cancellation took precedence over the worker error."})
            except Exception as finalize_exc:
                await _fail_task(task_id, f"Cancellation finalization failed ({finalize_exc.__class__.__name__})", repository, hub)
        else:
            await _fail_task(task_id, str(exc)[:1000] or exc.__class__.__name__, repository, hub)
    finally:
        if hasattr(repository, "release_task"):
            try:
                await _persist(repository, "release_task", task_id, worker_id)
            except Exception:
                pass


async def _cancel_requested(repository: Any, task_id: str) -> bool:
    task = await _persist(repository, "get_task", task_id)
    return bool(task and task.get("cancel_requested"))


async def _fail_task(task_id: str, message: str, repository: Any, hub: ProgressHub) -> None:
    safe_message = (
        message if message.startswith("Supabase ") or message.startswith("Task exceeded MAX_TASK_DURATION_SECONDS")
        else "Unexpected workflow failure; inspect the persisted step attempts for safe diagnostics."
    )
    try:
        await _persist(repository, "update_task", task_id, {
            "status": "FAILED", "error_summary": safe_message, "completed_at": utc_now(), "updated_at": utc_now(),
        })
    except Exception:
        pass
    try:
        if hasattr(repository, "append_event"):
            event = await _persist(repository, "append_event", task_id, "task_error", {"task_id": task_id, "status": "FAILED", "message": safe_message})
            await hub.publish(task_id, "task_error", {"task_id": task_id, "status": "FAILED", "message": safe_message}, event_id=event.get("sequence_no"))
        else:
            await hub.publish(task_id, "task_error", {"task_id": task_id, "status": "FAILED", "message": safe_message})
    except Exception:
        # Preserve the primary failure even when event persistence is unavailable.
        pass


async def _record_timeout_outcomes(task_id: str, repository: Any) -> None:
    """Close durable RUNNING rows when the overall task deadline cancels a provider call."""
    if not hasattr(repository, "list_attempts") or not hasattr(repository, "list_steps"):
        return
    now = utc_now()
    try:
        attempts = await _persist(repository, "list_attempts", task_id)
        for attempt in attempts:
            if attempt.get("status") == "RUNNING":
                await _persist(repository, "update_attempt", attempt["id"], {
                    "status": "FAILED", "error_classification": "timeout",
                    "safe_error": "Overall task deadline elapsed while provider work was in progress.",
                    "completed_at": now,
                })
        for step in await _persist(repository, "list_steps", task_id):
            if step.get("status") == "RUNNING":
                await _persist(repository, "update_step", task_id, step["action_key"], {
                    "status": "FAILED", "last_error_classification": "timeout",
                    "last_error": "Overall task deadline elapsed while provider work was in progress.",
                    "completed_at": now, "updated_at": now,
                })
    except Exception:
        # The timeout remains primary; the terminal task event still records it.
        return
