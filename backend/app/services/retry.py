"""Bounded retry classification for transient provider failures."""

import random
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
