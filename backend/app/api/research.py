import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import UUID, uuid4
from uuid import NAMESPACE_URL, uuid5
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from app.core.config import Settings, get_settings
from app.repositories.tasks import PersistenceError, SupabaseTaskRepository
from app.schemas.research import ResearchAccepted, ResearchRequest
from app.schemas.artifacts import ArtifactRequest
from app.services.artifacts import MIMES, artifact_filename, build_artifact, store_local
from app.services.ollama import check_ollama
from app.services.progress import ProgressHub, progress_hub
from app.services.research_workflow import execute_research

router = APIRouter(prefix="/research", tags=["research"])
artifact_router = APIRouter(tags=["artifacts"])


def get_repository(settings: Settings = Depends(get_settings)) -> SupabaseTaskRepository:
    if not settings.supabase_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "supabase_not_configured", "message": "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in backend/.env."},
        )
    try:
        return SupabaseTaskRepository(settings.supabase_url, settings.supabase_service_role_key)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "supabase_client_error", "message": f"Could not initialize Supabase ({exc.__class__.__name__})."}) from exc


def _has_active_worker(task: dict) -> bool:
    if not task.get("worker_id"):
        return False
    lease_value = task.get("lease_expires_at")
    try:
        lease_expiry = datetime.fromisoformat(lease_value.replace("Z", "+00:00")) if isinstance(lease_value, str) else lease_value
        if lease_expiry and lease_expiry.tzinfo is None:
            lease_expiry = lease_expiry.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return bool(lease_expiry and lease_expiry > datetime.now(timezone.utc))


async def require_research_providers(settings: Settings = Depends(get_settings)) -> Settings:
    missing = []
    if not settings.supabase_url or not settings.supabase_service_role_key:
        missing.extend(name for name, value in (("SUPABASE_URL", settings.supabase_url), ("SUPABASE_SERVICE_ROLE_KEY", settings.supabase_service_role_key)) if not value)
    if not settings.tavily_api_key:
        missing.append("TAVILY_API_KEY")
    if not settings.firecrawl_api_key:
        missing.append("FIRECRAWL_API_KEY")
    if missing:
        raise HTTPException(status_code=503, detail={"code": "providers_not_configured", "missing": missing, "message": "Configure the named variables in backend/.env to start live research."})
    ollama = await check_ollama(settings)
    if not ollama.get("connected") or not ollama.get("model_available"):
        raise HTTPException(status_code=503, detail={
            "code": "ollama_unavailable",
            "model": settings.ollama_model,
            "message": f"Ollama must be running with model {settings.ollama_model} installed. Check GET /api/health.",
        })
    return settings


@router.post("", response_model=ResearchAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_research(
    body: ResearchRequest,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(require_research_providers),
    repository: SupabaseTaskRepository = Depends(get_repository),
):
    if body.failure_injection and not (settings.failure_injection_enabled and settings.app_env in {"development", "test"}):
        raise HTTPException(status_code=403, detail={"code": "failure_injection_disabled", "message": "Failure injection is available only when explicitly enabled in development or test mode."})
    task_id = str(uuid4())
    worker_id = str(uuid4())
    row = {
        "id": task_id,
        "query": body.query,
        "output_type": body.output_type,
        "research_depth": body.research_depth,
        "status": "QUEUED",
        "config": {"target_count": body.target_count, **body.options.model_dump(mode="json"), "failure_injection": body.failure_injection},
    }
    try:
        await asyncio.to_thread(repository.create_task, row)
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail={"code": "supabase_write_failed", "message": str(exc)}) from exc
    try:
        await asyncio.to_thread(repository.append_event, task_id, "task_status", {"task_id": task_id, "status": "QUEUED", "message": "Task accepted and durably queued."})
        if hasattr(repository, "claim_task") and not await asyncio.to_thread(repository.claim_task, task_id, worker_id, resume=False):
            raise HTTPException(status_code=409, detail={"code": "task_claim_conflict", "message": "The task could not be claimed for execution."})
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail={"code": "task_start_failed", "message": str(exc)}) from exc
    background_tasks.add_task(execute_research, task_id, body, settings, repository, progress_hub, worker_id=worker_id)
    return ResearchAccepted(task_id=task_id, status="QUEUED", events_url=f"/api/research/{task_id}/events")


def _check_task_id(task_id: str) -> str:
    try:
        return str(UUID(task_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="task_id must be a UUID") from exc


async def _get_task_or_404(task_id: str, repository: SupabaseTaskRepository) -> dict:
    try:
        task = await asyncio.to_thread(repository.get_task, _check_task_id(task_id))
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail={"code": "supabase_read_failed", "message": str(exc)}) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="Research task not found")
    return task


async def _resume_eligibility(task: dict, repository: SupabaseTaskRepository) -> bool:
    task_id = task["id"]
    if task.get("status") in {"COMPLETED", "CANCELLED"}:
        return False
    steps, sources = await asyncio.gather(
        asyncio.to_thread(repository.list_steps, task_id),
        asyncio.to_thread(repository.list_sources, task_id),
    )
    unresolved = any(step.get("status") in {"PENDING", "RUNNING", "RETRYING"} for step in steps)
    safe_failed = any(step.get("status") == "FAILED" and step.get("last_error_classification") not in {"authorization", "validation"} for step in steps)
    step_by_key = {step.get("action_key"): step for step in steps}
    recoverable_source = False
    for source in sources:
        key = "read_" + hashlib.sha256(source["canonical_url"].encode()).hexdigest()[:24]
        read_step = step_by_key.get(key)
        if read_step is None or read_step.get("status") in {"PENDING", "RUNNING", "RETRYING"}:
            recoverable_source = True
            break
        if read_step.get("status") == "FAILED" and read_step.get("last_error_classification") not in {"authorization", "validation"}:
            recoverable_source = True
            break
    if not (unresolved or safe_failed or recoverable_source or (task.get("status") in {"QUEUED", "PLANNING"} and task.get("plan") is None) or (task.get("status") in {"RUNNING", "RECOVERING"} and task.get("plan") is not None)):
        return False
    try:
        expiry = datetime.fromisoformat(str(task.get("lease_expires_at")).replace("Z", "+00:00"))
        if task.get("worker_id") and expiry > datetime.now(timezone.utc):
            return False
    except (TypeError, ValueError):
        pass
    return task.get("status") in {"QUEUED", "PLANNING", "FAILED", "COMPLETED_WITH_LIMITATIONS", "RUNNING", "RECOVERING"}


@router.get("/{task_id}")
async def get_research(task_id: str, repository: SupabaseTaskRepository = Depends(get_repository)):
    task = await _get_task_or_404(task_id, repository)
    try:
        task["resume_eligible"] = await _resume_eligibility(task, repository)
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail={"code": "recovery_state_read_failed", "message": str(exc)}) from exc
    task["cancel_eligible"] = task.get("status") in {"QUEUED", "PLANNING", "RUNNING", "RECOVERING"} and not task.get("cancel_requested")
    return task


@router.get("/{task_id}/sources")
async def get_sources(task_id: str, repository: SupabaseTaskRepository = Depends(get_repository)):
    task = await _get_task_or_404(task_id, repository)
    try:
        sources = await asyncio.to_thread(repository.list_sources, task["id"])
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail={"code": "supabase_read_failed", "message": str(exc)}) from exc
    return {"sources": sources}


@router.get("/{task_id}/findings")
async def get_findings(task_id: str, repository: SupabaseTaskRepository = Depends(get_repository)):
    task = await _get_task_or_404(task_id, repository)
    try:
        findings = await asyncio.to_thread(repository.list_findings, task["id"])
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail={"code": "supabase_read_failed", "message": str(exc)}) from exc
    return {"findings": findings}


def _artifact_public(row: dict) -> dict:
    return {key: row.get(key) for key in ("id", "task_id", "artifact_type", "filename", "mime_type", "status", "preview", "created_at")}


@router.get("/{task_id}/artifacts")
async def list_research_artifacts(task_id: str, repository: SupabaseTaskRepository = Depends(get_repository)):
    task = await _get_task_or_404(task_id, repository)
    try:
        return {"artifacts": [_artifact_public(row) for row in await asyncio.to_thread(repository.list_artifacts, task["id"])]}
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail={"code": "artifact_read_failed", "message": str(exc)}) from exc


@router.post("/{task_id}/artifacts", status_code=201)
async def create_research_artifact(task_id: str, body: ArtifactRequest, settings: Settings = Depends(get_settings), repository: SupabaseTaskRepository = Depends(get_repository)):
    task = await _get_task_or_404(task_id, repository)
    if task.get("status") == "CANCELLED":
        raise HTTPException(status_code=409, detail={"code": "task_cancelled", "message": "Artifacts cannot be generated for a cancelled task."})
    if body.recipient and not __import__("re").fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", body.recipient):
        raise HTTPException(status_code=422, detail="recipient must be a valid email address")
    try:
        sources, findings = await asyncio.gather(
            asyncio.to_thread(repository.list_sources, task["id"]),
            asyncio.to_thread(repository.list_findings, task["id"]),
        )
        payload, preview = build_artifact(task, findings, sources, body.artifact_type, body.recipient)
        generation_key = f"{body.artifact_type}:{hashlib.sha256((body.recipient or '').encode()).hexdigest()[:16]}"
        artifact_id = str(uuid5(NAMESPACE_URL, f"proofloop:{task['id']}:{generation_key}"))
        filename = artifact_filename(body.artifact_type, task["id"])
        backend = settings.artifact_storage_backend
        if backend == "auto":
            backend = "supabase" if settings.supabase_configured else "local"
        if backend == "supabase":
            if not settings.supabase_configured:
                raise HTTPException(status_code=503, detail={"code": "artifact_storage_not_configured", "message": "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY or set ARTIFACT_STORAGE_BACKEND=local."})
            storage_path = f"{task['id']}/{artifact_id}/{filename}"
            await asyncio.to_thread(repository.upload_artifact, settings.supabase_storage_bucket, storage_path, payload, MIMES[body.artifact_type])
        else:
            storage_path = await asyncio.to_thread(store_local, settings.artifact_dir, artifact_id, payload)
        row = {
            "id": artifact_id, "task_id": task["id"], "artifact_type": body.artifact_type,
            "generation_key": generation_key, "filename": filename, "storage_backend": backend,
            "storage_path": storage_path, "mime_type": MIMES[body.artifact_type], "status": "READY",
            "preview": preview, "content_hash": hashlib.sha256(payload).hexdigest(),
        }
        saved = await asyncio.to_thread(repository.save_artifact, row)
        try:
            await asyncio.to_thread(repository.append_event, task["id"], "artifact_generated", {"artifact_id": artifact_id, "artifact_type": body.artifact_type, "status": "READY"})
        except Exception:
            pass
        return _artifact_public(saved)
    except HTTPException:
        raise
    except PersistenceError as exc:
        message = str(exc)
        if "artifacts" in message.lower() or "PGRST205" in message:
            message = "Artifact table is unavailable. Apply backend/supabase/migrations/20261019_stage4_artifacts.sql to the configured Supabase project and refresh the PostgREST schema cache."
        raise HTTPException(status_code=503, detail={"code": "artifact_persistence_failed", "message": message}) from exc


@artifact_router.get("/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: str, settings: Settings = Depends(get_settings), repository: SupabaseTaskRepository = Depends(get_repository)):
    try:
        artifact_id = str(UUID(artifact_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="artifact_id must be a UUID") from exc
    try:
        row = await asyncio.to_thread(repository.get_artifact, artifact_id)
        if not row:
            raise HTTPException(status_code=404, detail="Artifact not found")
        if row.get("storage_backend") == "supabase":
            url = await asyncio.to_thread(repository.signed_artifact_url, settings.supabase_storage_bucket, row["storage_path"], 300)
            if not url:
                raise HTTPException(status_code=503, detail={"code": "artifact_url_failed", "message": "Supabase did not return a signed download URL."})
            return RedirectResponse(url, status_code=307)
        root = Path(settings.artifact_dir).expanduser().resolve()
        path = Path(row["storage_path"]).resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="Artifact file is unavailable")
        return FileResponse(path, media_type=row["mime_type"], filename=row["filename"])
    except HTTPException:
        raise
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail={"code": "artifact_download_failed", "message": str(exc)}) from exc


@router.post("/{task_id}/resume", response_model=ResearchAccepted, status_code=status.HTTP_202_ACCEPTED)
async def resume_research(
    task_id: str,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(require_research_providers),
    repository: SupabaseTaskRepository = Depends(get_repository),
):
    task = await _get_task_or_404(task_id, repository)
    task_id = task["id"]
    if task.get("cancel_requested") or task.get("status") == "CANCELLED":
        if task.get("status") != "CANCELLED" and not task.get("worker_id"):
            try:
                await asyncio.to_thread(repository.finalize_pending_cancellations)
            except PersistenceError as exc:
                raise HTTPException(status_code=503, detail={"code": "cancellation_recovery_failed", "message": str(exc)}) from exc
        raise HTTPException(status_code=409, detail={"code": "task_cancelled", "message": "Cancellation is pending or complete; this task will not be resumed."})
    if task.get("status") in {"COMPLETED", "CANCELLED"}:
        raise HTTPException(status_code=409, detail={"code": "task_not_resumable", "message": f"A {task.get('status')} task cannot be resumed."})
    if task.get("status") not in {"QUEUED", "PLANNING", "FAILED", "COMPLETED_WITH_LIMITATIONS", "RUNNING", "RECOVERING"}:
        raise HTTPException(status_code=409, detail={"code": "task_not_resumable", "message": "Only interrupted or recoverable tasks can be resumed."})
    try:
        steps = await asyncio.to_thread(repository.list_steps, task_id)
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail={"code": "supabase_read_failed", "message": str(exc)}) from exc
    unresolved = [step for step in steps if step.get("status") in {"PENDING", "RUNNING", "RETRYING"}]
    safe_failed = [step for step in steps if step.get("status") == "FAILED" and step.get("last_error_classification") not in {"authorization", "validation"}]
    try:
        sources = await asyncio.to_thread(repository.list_sources, task_id)
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail={"code": "supabase_read_failed", "message": str(exc)}) from exc
    step_by_key = {step.get("action_key"): step for step in steps}
    recoverable_sources = []
    for source in sources:
        key = "read_" + hashlib.sha256(source["canonical_url"].encode()).hexdigest()[:24]
        read_step = step_by_key.get(key)
        if read_step is None or read_step.get("status") in {"PENDING", "RUNNING", "RETRYING"}:
            recoverable_sources.append(source)
        elif read_step.get("status") == "FAILED" and read_step.get("last_error_classification") not in {"authorization", "validation"}:
            recoverable_sources.append(source)
    if not unresolved and not safe_failed and not recoverable_sources and not (task.get("status") in {"QUEUED", "PLANNING"} and task.get("plan") is None) and task.get("status") not in {"RUNNING", "RECOVERING"}:
        raise HTTPException(status_code=409, detail={"code": "task_not_resumable", "message": "The task has no unfinished or safely retryable steps."})
    worker_id = str(uuid4())
    try:
        claimed = await asyncio.to_thread(repository.claim_task, task_id, worker_id, resume=True)
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail={"code": "task_claim_failed", "message": str(exc)}) from exc
    if not claimed:
        latest = await _get_task_or_404(task_id, repository)
        try:
            lease_expiry = datetime.fromisoformat(str(latest.get("lease_expires_at")).replace("Z", "+00:00"))
            active_lease = bool(latest.get("worker_id")) and lease_expiry > datetime.now(timezone.utc)
        except (TypeError, ValueError):
            active_lease = False
        if active_lease:
            raise HTTPException(status_code=409, detail={"code": "task_already_running", "message": "Another worker holds the task lease."})
        raise HTTPException(status_code=409, detail={
            "code": "task_claim_rejected",
            "message": "Supabase rejected the recovery claim although no worker lease is active. Apply backend/supabase/migrations/20261017_stage3_resume_claim.sql, then retry resume.",
        })
    body = ResearchRequest(
        query=task["query"], output_type=task.get("output_type", "auto"),
        research_depth=task.get("research_depth", "standard"),
        target_count=(task.get("config") or {}).get("target_count"),
        options={key: (task.get("config") or {}).get(key) for key in ("location", "domain", "required_fields")},
        failure_injection=(task.get("config") or {}).get("failure_injection"),
    )
    try:
        await asyncio.to_thread(repository.append_event, task_id, "task_resumed", {"task_id": task_id, "status": "RECOVERING"})
    except PersistenceError as exc:
        try:
            await asyncio.to_thread(repository.update_task, task_id, {"status": "FAILED", "error_summary": "Recovery could not be recorded in durable event history."})
            await asyncio.to_thread(repository.release_task, task_id, worker_id)
        except Exception:
            pass
        raise HTTPException(status_code=503, detail={"code": "event_write_failed", "message": str(exc)}) from exc
    background_tasks.add_task(execute_research, task_id, body, settings, repository, progress_hub, resume=True, worker_id=worker_id)
    return ResearchAccepted(task_id=task_id, status="RECOVERING", events_url=f"/api/research/{task_id}/events")


@router.post("/{task_id}/cancel")
async def cancel_research(task_id: str, repository: SupabaseTaskRepository = Depends(get_repository)):
    task = await _get_task_or_404(task_id, repository)
    if task.get("status") == "CANCELLED":
        return {"task_id": task["id"], "status": "CANCELLED"}
    if task.get("cancel_requested"):
        if not _has_active_worker(task):
            try:
                await asyncio.to_thread(repository.finalize_pending_cancellations)
            except PersistenceError as exc:
                raise HTTPException(status_code=503, detail={"code": "cancel_failed", "message": str(exc)}) from exc
            current = await _get_task_or_404(task["id"], repository)
            if current.get("status") == "CANCELLED":
                return {"task_id": task["id"], "status": "CANCELLED"}
        return {"task_id": task["id"], "status": "CANCELLATION_REQUESTED"}
    if task.get("status") in {"COMPLETED", "COMPLETED_WITH_LIMITATIONS", "FAILED"}:
        raise HTTPException(status_code=409, detail={"code": "task_not_cancellable", "message": f"A {task.get('status')} task cannot be cancelled."})
    try:
        requested = await asyncio.to_thread(repository.request_cancel, task["id"])
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail={"code": "cancel_failed", "message": str(exc)}) from exc
    if not requested:
        raise HTTPException(status_code=409, detail={"code": "task_not_cancellable", "message": "Cancellation was not accepted by the current task state."})
    current = await _get_task_or_404(task["id"], repository)
    if not _has_active_worker(current):
        try:
            await asyncio.to_thread(repository.finalize_pending_cancellations)
        except PersistenceError as exc:
            raise HTTPException(status_code=503, detail={"code": "cancel_failed", "message": str(exc)}) from exc
        current = await _get_task_or_404(task["id"], repository)
        if current.get("status") == "CANCELLED":
            return {"task_id": task["id"], "status": "CANCELLED"}
    return {"task_id": task["id"], "status": "CANCELLATION_REQUESTED"}


@router.get("/{task_id}/events")
async def get_research_events(
    task_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    repository: SupabaseTaskRepository = Depends(get_repository),
):
    task = await _get_task_or_404(task_id, repository)
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))
    cursor = after
    history_id = last_event_id
    if history_id and history_id.isdigit():
        cursor = max(cursor, int(history_id))
    terminal_statuses = {"COMPLETED", "COMPLETED_WITH_LIMITATIONS", "FAILED", "CANCELLED"}

    async def events() -> AsyncIterator[str]:
        nonlocal cursor
        idle_polls = 0
        terminal_empty_polls = 0
        stream_deadline = asyncio.get_running_loop().time() + get_settings().max_sse_stream_seconds
        while not await request.is_disconnected() and asyncio.get_running_loop().time() < stream_deadline:
            try:
                pending = await asyncio.to_thread(repository.list_events, task["id"], cursor, 100)
            except PersistenceError as exc:
                data = {"code": "event_read_failed", "message": str(exc)}
                yield f"event: stream_error\ndata: {json.dumps(data)}\n\n"
                return
            for event in pending:
                cursor = int(event["sequence_no"])
                payload = event.get("payload") or {}
                yield f"event: {event['event_type']}\nid: {cursor}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if pending:
                idle_polls = 0
                terminal_empty_polls = 0
                continue
            current = await _get_task_or_404(task["id"], repository)
            if current.get("status") in terminal_statuses:
                terminal_empty_polls += 1
                if terminal_empty_polls >= 2:
                    return
            else:
                terminal_empty_polls = 0
            idle_polls += 1
            if idle_polls >= 15:
                yield ": keep-alive\n\n"
                idle_polls = 0
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
