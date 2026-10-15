import ipaddress
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.research import artifact_router, router as research_router, source_router
from app.core.config import get_settings
from app.repositories.tasks import SupabaseTaskRepository

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # A restart must finalize abandoned cancellation requests before any task
    # can be resumed. Never auto-resume research work at startup.
    if settings.app_env != "test" and settings.supabase_configured:
        try:
            repository = SupabaseTaskRepository(settings.supabase_url, settings.supabase_service_role_key)
            recovered = await asyncio.to_thread(repository.finalize_pending_cancellations)
            if recovered:
                logger.info("Finalized %d pending research cancellation(s) after startup", recovered)
        except Exception as exc:
            logger.error("Could not finalize pending research cancellations at startup (%s)", exc.__class__.__name__)
    yield


app = FastAPI(title="ProofLoop API", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def restrict_single_user_api_to_loopback(request: Request, call_next):
    """The service-role API remains local-only until authenticated ownership exists."""
    host = request.client.host if request.client else ""
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "testclient"
    if not local:
        return JSONResponse(status_code=403, content={"detail": {"code": "local_access_only", "message": "This single-user API is available only from the local machine."}})
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(health_router, prefix="/api")
app.include_router(research_router, prefix="/api")
app.include_router(source_router, prefix="/api")
app.include_router(artifact_router, prefix="/api")
