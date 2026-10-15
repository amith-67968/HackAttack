from typing import Any
from pydantic import BaseModel

import httpx

from app.core.config import Settings


async def check_ollama(settings: Settings) -> dict[str, Any]:
    """Check the local Ollama API and whether the selected model is installed."""
    try:
        async with httpx.AsyncClient(timeout=min(settings.ollama_timeout_seconds, 5.0)) as client:
            response = await client.get(f"{settings.ollama_base_url}/api/tags")
            response.raise_for_status()
        payload = response.json()
        models = payload.get("models", [])
        available_models = [item.get("name", "") for item in models if isinstance(item, dict)]
        selected = settings.ollama_model
        model_available = any(
            name == selected or name.split(":", 1)[0] == selected.split(":", 1)[0]
            and selected.count(":") == 0
            for name in available_models
        )
        return {
            "status": "available" if model_available else "model_missing",
            "connected": True,
            "model": selected,
            "model_available": model_available,
            "available_models": available_models,
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "status": "unavailable",
            "connected": False,
            "model": settings.ollama_model,
            "model_available": False,
            "available_models": [],
            "detail": str(exc) or exc.__class__.__name__,
        }


class OllamaError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, retry_after_seconds: float | str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


async def generate_structured(
    settings: Settings,
    *,
    system: str,
    prompt: str,
    schema: type[BaseModel],
    transport: httpx.AsyncBaseTransport | None = None,
) -> BaseModel:
    """Generate JSON from the configured local model and validate it with Pydantic."""
    try:
        async with httpx.AsyncClient(timeout=settings.ollama_timeout_seconds, transport=transport) as client:
            response = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "system": system,
                    "prompt": prompt,
                    "format": schema.model_json_schema(),
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0},
                },
            )
            response.raise_for_status()
        text = response.json().get("response", "")
        if not text:
            raise OllamaError("Ollama returned an empty response")
        return schema.model_validate_json(text)
    except OllamaError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        reason = "model not found" if status == 404 else "request or response validation failed"
        response = getattr(exc, "response", None)
        retry_after = getattr(response, "headers", {}).get("Retry-After") if response is not None else None
        raise OllamaError(f"Ollama {reason}: {status or exc.__class__.__name__}", status, retry_after) from exc
