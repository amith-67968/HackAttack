from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.services.ollama import check_ollama

router = APIRouter()


@router.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict:
    ollama = await check_ollama(settings)
    return {
        "status": "ok",
        "components": {
            "api": {"status": "available"},
            "ollama": ollama,
            "supabase": {
                "status": "configured" if settings.supabase_configured else "not_configured",
                "connected": False,
            },
            "tavily": {"status": "configured" if settings.tavily_api_key else "not_configured"},
            "firecrawl": {"status": "configured" if settings.firecrawl_api_key else "not_configured"},
            "recovery": {
                "failure_injection_enabled": bool(settings.failure_injection_enabled and settings.app_env in {"development", "test"}),
                "max_provider_attempts": settings.max_provider_attempts,
            },
        },
    }
