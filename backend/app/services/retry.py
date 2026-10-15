"""Bounded retry classification for transient provider failures."""

import random
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


@dataclass(frozen=True)
class ErrorClassification:
    name: str
    retryable: bool
    retry_after_seconds: float | None = None


def _retry_after(exc: BaseException) -> float | None:
    value = getattr(exc, "retry_after_seconds", None)
    if value is not None:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}).get("Retry-After") if response is not None else None
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def classify_error(exc: BaseException) -> ErrorClassification:
    """Classify provider errors without including their messages or response bodies."""
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    retry_after = _retry_after(exc)

    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return ErrorClassification("timeout", True, retry_after)
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError, ConnectionError, OSError)):
        return ErrorClassification("connection", True, retry_after)
    if status == 429:
        return ErrorClassification("rate_limited", True, retry_after)
    if status in {408, 425, 500, 502, 503, 504}:
        return ErrorClassification("temporary_server_error", True, retry_after)
    if status in {401, 403}:
        return ErrorClassification("authorization", False)
    if status is not None and 400 <= status < 500:
        return ErrorClassification("validation", False)
    if isinstance(exc, (ValueError, TypeError)):
        return ErrorClassification("validation", False)
    return ErrorClassification("unknown", False)


def retry_delay(attempt_no: int, *, base_seconds: float, max_seconds: float, jitter: Any = random.uniform) -> float:
    """Exponential backoff with bounded jitter; attempt_no is the failed attempt."""
    ceiling = min(max_seconds, base_seconds * (2 ** max(0, attempt_no - 1)))
    return min(max_seconds, max(0.0, float(jitter(ceiling * 0.8, ceiling * 1.2))))


def safe_error_text(exc: BaseException, classification: ErrorClassification) -> str:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    return f"{classification.name} (HTTP {status})" if status is not None else f"{classification.name} provider failure"


async def run_with_retries(
    operation: Any,
    *,
    max_attempts: int,
    base_seconds: float,
    max_seconds: float,
    on_start: Any,
    on_attempt: Any,
    on_retry: Any,
    sleep: Any = asyncio.sleep,
    jitter: Any = random.uniform,
) -> Any:
    """Run one action with bounded retries; callbacks persist attempt outcomes."""
    for attempt_no in range(1, max_attempts + 1):
        await on_start(attempt_no)
        try:
            result = await operation()
        except Exception as exc:
            classification = classify_error(exc)
            await on_attempt(attempt_no, False, classification, safe_error_text(exc, classification))
            if not classification.retryable or attempt_no >= max_attempts:
                raise
            delay = classification.retry_after_seconds
            if delay is None:
                delay = retry_delay(attempt_no, base_seconds=base_seconds, max_seconds=max_seconds, jitter=jitter)
            await on_retry(attempt_no, classification, delay)
            await sleep(delay)
        else:
            await on_attempt(attempt_no, True, None, None)
            return result
    raise RuntimeError("Retry loop exhausted without a result")
