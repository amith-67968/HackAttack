import asyncio
import json
from typing import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.core.config import Settings, get_settings
from app.repositories.tasks import PersistenceError, SupabaseTaskRepository
from app.schemas.research import ResearchAccepted, ResearchRequest
from app.services.ollama import check_ollama
from app.services.progress import ProgressHub, progress_hub
from app.services.research_workflow import execute_research

router = APIRouter(prefix="/research", tags=["research"])


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
    from uuid import uuid4

    task_id = str(uuid4())
    row = {
        "id": task_id,
        "query": body.query,
        "output_type": body.output_type,
        "research_depth": body.research_depth,
        "status": "QUEUED",
        "config": {"target_count": body.target_count, **body.options.model_dump(mode="json")},
    }
    try:
        await asyncio.to_thread(repository.create_task, row)
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail={"code": "supabase_write_failed", "message": str(exc)}) from exc
    background_tasks.add_task(execute_research, task_id, body, settings, repository, progress_hub)
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


@router.get("/{task_id}")
async def get_research(task_id: str, repository: SupabaseTaskRepository = Depends(get_repository)):
    return await _get_task_or_404(task_id, repository)


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


@router.get("/{task_id}/events")
async def get_research_events(
    task_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    repository: SupabaseTaskRepository = Depends(get_repository),
):
    task = await _get_task_or_404(task_id, repository)
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))
    history = progress_hub.history(task["id"])
    terminal_statuses = {"COMPLETED", "COMPLETED_WITH_LIMITATIONS", "FAILED", "CANCELLED"}
    terminal_events = {"task_completed", "task_error"}

    async def events() -> AsyncIterator[str]:
        if task.get("status") in terminal_statuses and not any(event["event"] in terminal_events for event in history):
            kind = "task_error" if task["status"] == "FAILED" else "task_completed"
            data = {"task_id": task["id"], "status": task["status"], "summary": task.get("summary"), "message": task.get("error_summary")}
            yield f"event: {kind}\nid: 1\ndata: {json.dumps(data)}\n\n"
            return
        terminal_ids = [event["id"] for event in history if event["event"] in terminal_events]
        if task.get("status") in terminal_statuses and terminal_ids and after >= terminal_ids[-1]:
            return
        async for event in progress_hub.stream(task["id"], after):
            yield f"event: {event['event']}\nid: {event['id']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
