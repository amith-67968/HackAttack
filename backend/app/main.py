import ipaddress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.research import router as research_router
from app.core.config import get_settings

settings = get_settings()
app = FastAPI(title="ProofLoop API", version="0.1.0")


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
