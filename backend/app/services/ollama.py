import asyncio
import json
import threading
import weakref
from collections.abc import Callable
from typing import Any
from pydantic import BaseModel
from pydantic import ValidationError

import httpx

from app.core.config import Settings


_semaphore_lock = threading.Lock()
_semaphores: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _request_semaphore(limit: int) -> asyncio.Semaphore:
    """Limit model calls across all tasks sharing one application event loop."""
    loop = asyncio.get_running_loop()
    with _semaphore_lock:
        semaphore = _semaphores.get(loop)
        if semaphore is None or getattr(semaphore, "_proofloop_limit", None) != limit:
            semaphore = asyncio.Semaphore(limit)
            semaphore._proofloop_limit = limit
            _semaphores[loop] = semaphore
        return semaphore


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
    def __init__(self, message: str, status_code: int | None = None, retry_after_seconds: float | str | None = None, *, failure_kind: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.failure_kind = failure_kind


async def generate_structured(
    settings: Settings,
    *,
    system: str,
    prompt: str,
    schema: type[BaseModel],
    transport: httpx.AsyncBaseTransport | None = None,
    normalize: Callable[[Any], Any] | None = None,
) -> BaseModel:
    """Generate JSON from the configured local model and validate it with Pydantic."""
    try:
        async with _request_semaphore(settings.max_ollama_concurrency):
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
                        "options": {"temperature": 0, "num_ctx": settings.ollama_num_ctx},
                    },
                )
                response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise OllamaError("Ollama returned an invalid response object", failure_kind="validation")
            text = payload.get("response", "")
            if not isinstance(text, str) or not text.strip():
                raise OllamaError("Ollama returned an empty response", failure_kind="validation")
            parsed = json.loads(text)
            if normalize:
                parsed = normalize(parsed)
            return schema.model_validate(parsed)
    except OllamaError:
        raise
    except ValidationError as exc:
        raise OllamaError("Ollama returned structured output that failed schema validation", failure_kind="validation") from exc
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        reason = "model not found" if status == 404 else "request or response validation failed"
        response = getattr(exc, "response", None)
        retry_after = getattr(response, "headers", {}).get("Retry-After") if response is not None else None
        # A local model generation that exceeded its full response deadline is
        # expensive to repeat with the same prompt and model. Let the workflow
        # mark this source as limited and continue with the remaining sources.
        # Connection errors remain retryable because they can be transient.
        kind = "timeout" if isinstance(exc, httpx.TimeoutException) else (
            "provider" if isinstance(exc, httpx.HTTPError) else "validation"
        )
        raise OllamaError(f"Ollama {reason}: {status or exc.__class__.__name__}", status, retry_after, failure_kind=kind) from exc
