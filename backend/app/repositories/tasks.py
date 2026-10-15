"""Supabase persistence for the Stage 2 task and evidence workflow."""

import time
from typing import Any, Protocol

from supabase import Client, create_client

from app.core.config import get_settings
from app.services.retry import classify_error, retry_delay


class PersistenceError(RuntimeError):
    def __init__(self, message: str, *, provider_code: str | None = None) -> None:
        super().__init__(message)
        self.provider_code = provider_code


class TaskRepository(Protocol):
    def create_task(self, row: dict[str, Any]) -> dict[str, Any]: ...
    def update_task(self, task_id: str, row: dict[str, Any]) -> None: ...
    def create_step(self, row: dict[str, Any]) -> dict[str, Any]: ...
    def update_step(self, task_id: str, action_key: str, row: dict[str, Any]) -> None: ...
    def upsert_source(self, row: dict[str, Any]) -> dict[str, Any]: ...
    def update_source(self, source_id: str, row: dict[str, Any]) -> None: ...
    def upsert_findings(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]: ...
    def save_findings_if_active(self, task_id: str, worker_id: str, rows: list[dict[str, Any]]) -> bool: ...
    def start_attempt(self, task_id: str, worker_id: str, action_key: str, attempt_no: int, tool_name: str) -> dict[str, Any] | None: ...
    def finish_cancellation(self, task_id: str, worker_id: str) -> bool: ...
    def finalize_pending_cancellations(self) -> int: ...
    def get_task(self, task_id: str) -> dict[str, Any] | None: ...
    def list_sources(self, task_id: str) -> list[dict[str, Any]]: ...
    def list_findings(self, task_id: str) -> list[dict[str, Any]]: ...
    def list_steps(self, task_id: str) -> list[dict[str, Any]]: ...
    def list_events(self, task_id: str, after: int = 0, limit: int = 100) -> list[dict[str, Any]]: ...
    def append_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class SupabaseTaskRepository:
    # These operations are SELECTs and can be repeated safely after a transport
    # failure. Writes and RPCs are intentionally excluded because a lost response
    # can leave their outcome ambiguous.
    _READ_OPERATIONS = frozenset({
        "task lookup", "step lookup", "step listing", "attempt listing",
        "source lookup", "source listing", "finding listing", "event listing",
    })

    def __init__(self, url: str, service_role_key: str, *, client: Client | None = None) -> None:
        if client is None and not (url and service_role_key):
            raise PersistenceError("Supabase is not configured; set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
        self._client = client or create_client(url, service_role_key)

    def _execute(self, operation: str, query: Any) -> list[dict[str, Any]]:
        settings = get_settings()
        attempts = settings.max_provider_attempts if operation in self._READ_OPERATIONS else 1
        for attempt_no in range(1, attempts + 1):
            try:
                response = query.execute()
                return response.data or []
            except Exception as exc:
                classification = classify_error(exc)
                if classification.retryable and attempt_no < attempts:
                    delay = classification.retry_after_seconds
                    if delay is None:
                        delay = retry_delay(
                            attempt_no,
                            base_seconds=settings.retry_base_delay_seconds,
                            max_seconds=settings.retry_max_delay_seconds,
                        )
                    time.sleep(delay)
                    continue

                # Do not pass provider URLs, request headers, response bodies,
                # or credentials to the UI.
                provider_code = getattr(exc, "code", None)
                if provider_code == "PGRST205":
                    message = (
                        f"Supabase {operation} failed (PGRST205): the table is unavailable in the current "
                        "PostgREST schema. Apply the Stage 2 migration to the configured project; if it is "
                        "already applied, confirm the public schema is exposed and reload the API schema cache."
                    )
                else:
                    safe_code = provider_code if isinstance(provider_code, str) and len(provider_code) <= 40 else exc.__class__.__name__
                    message = f"Supabase {operation} failed ({safe_code})"
                raise PersistenceError(message, provider_code=provider_code if isinstance(provider_code, str) else None) from exc

    def create_task(self, row: dict[str, Any]) -> dict[str, Any]:
        data = self._execute("task creation", self._client.table("research_tasks").insert(row))
        if not data:
            raise PersistenceError("Supabase task creation returned no task row")
        return data[0]

    def update_task(self, task_id: str, row: dict[str, Any]) -> None:
        self._execute("task update", self._client.table("research_tasks").update(row).eq("id", task_id))

    def create_step(self, row: dict[str, Any]) -> dict[str, Any]:
        data = self._execute(
            "step creation",
            self._client.table("research_steps").upsert(row, on_conflict="task_id,action_key"),
        )
        return data[0] if data else row

    def update_step(self, task_id: str, action_key: str, row: dict[str, Any]) -> None:
        self._execute(
            "step update",
            self._client.table("research_steps").update(row).eq("task_id", task_id).eq("action_key", action_key),
        )

    def get_step(self, task_id: str, action_key: str) -> dict[str, Any] | None:
        data = self._execute(
            "step lookup", self._client.table("research_steps").select("*").eq("task_id", task_id).eq("action_key", action_key).limit(1)
        )
        return data[0] if data else None

    def list_steps(self, task_id: str) -> list[dict[str, Any]]:
        return self._execute("step listing", self._client.table("research_steps").select("*").eq("task_id", task_id).order("sequence_no"))

    def create_attempt(self, row: dict[str, Any]) -> dict[str, Any]:
        data = self._execute("attempt creation", self._client.table("research_step_attempts").insert(row))
        if not data:
            raise PersistenceError("Supabase attempt creation returned no attempt row")
        return data[0]

    def update_attempt(self, attempt_id: str, row: dict[str, Any]) -> None:
        self._execute("attempt update", self._client.table("research_step_attempts").update(row).eq("id", attempt_id))

    def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        return self._execute("attempt listing", self._client.table("research_step_attempts").select("*").eq("task_id", task_id).order("started_at"))

    def upsert_source(self, row: dict[str, Any]) -> dict[str, Any]:
        data = self._execute(
            "source save",
            # PostgREST upsert returns the representation by default. The returned
            # request builder does not support chaining select() in current clients.
            self._client.table("sources").upsert(row, on_conflict="task_id,canonical_url"),
        )
        if not data:
            # Some compatible Supabase clients return no representation for upsert.
            existing = self._execute(
                "source lookup",
                self._client.table("sources").select("*").eq("task_id", row["task_id"]).eq("canonical_url", row["canonical_url"]).limit(1),
            )
            if not existing:
                raise PersistenceError("Supabase source save returned no source row")
            return existing[0]
        return data[0]

    def update_source(self, source_id: str, row: dict[str, Any]) -> None:
        self._execute("source update", self._client.table("sources").update(row).eq("id", source_id))

    def upsert_findings(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        return self._execute(
            "finding save",
            self._client.table("findings").upsert(rows, on_conflict="source_id,entity_key,field_name"),
        )

    def save_findings_if_active(self, task_id: str, worker_id: str, rows: list[dict[str, Any]]) -> bool:
        try:
            data = self._client.rpc("save_research_findings_if_active", {
                "p_task_id": task_id, "p_worker_id": worker_id, "p_rows": rows,
            }).execute().data
            if isinstance(data, list): data = data[0] if data else False
            if isinstance(data, dict): data = next(iter(data.values()), False)
            return data is True
        except Exception as exc:
            raise PersistenceError(f"Supabase finding save failed ({exc.__class__.__name__})") from exc

    def start_attempt(self, task_id: str, worker_id: str, action_key: str, attempt_no: int, tool_name: str) -> dict[str, Any] | None:
        try:
            data = self._client.rpc("start_research_attempt", {
                "p_task_id": task_id, "p_worker_id": worker_id, "p_action_key": action_key,
                "p_attempt_no": attempt_no, "p_tool_name": tool_name,
            }).execute().data
            if isinstance(data, list): data = data[0] if data else None
            return data if isinstance(data, dict) else None
        except Exception as exc:
            raise PersistenceError(f"Supabase attempt start failed ({exc.__class__.__name__})") from exc

    def finish_cancellation(self, task_id: str, worker_id: str) -> bool:
        try:
            data = self._client.rpc("finish_research_cancellation", {
                "p_task_id": task_id, "p_worker_id": worker_id,
            }).execute().data
            if isinstance(data, list): data = data[0] if data else False
            if isinstance(data, dict): data = next(iter(data.values()), False)
            return data is True
        except Exception as exc:
            raise PersistenceError(f"Supabase cancellation finalization failed ({exc.__class__.__name__})") from exc

    def finalize_pending_cancellations(self) -> int:
        try:
            data = self._client.rpc("finalize_pending_research_cancellations").execute().data
            if isinstance(data, list): data = data[0] if data else 0
            if isinstance(data, dict): data = next(iter(data.values()), 0)
            return int(data or 0)
        except Exception as exc:
            raise PersistenceError(f"Supabase pending cancellation recovery failed ({exc.__class__.__name__})") from exc

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        data = self._execute("task lookup", self._client.table("research_tasks").select("*").eq("id", task_id).limit(1))
        return data[0] if data else None

    def list_sources(self, task_id: str) -> list[dict[str, Any]]:
        return self._execute("source listing", self._client.table("sources").select("*").eq("task_id", task_id).order("created_at"))

    def list_findings(self, task_id: str) -> list[dict[str, Any]]:
        return self._execute("finding listing", self._client.table("findings").select("*").eq("task_id", task_id).order("created_at"))

    def append_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.rpc("append_research_task_event", {
                "p_task_id": task_id, "p_event_type": event_type, "p_payload": payload,
            }).execute()
            rows = response.data or []
            if isinstance(rows, dict):
                return {**rows, "event_type": event_type, "payload": payload}
            if rows:
                return {**rows[0], "event_type": event_type, "payload": payload}
            raise PersistenceError("Supabase event append returned no sequence")
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError(f"Supabase event append failed ({exc.__class__.__name__})") from exc

    def list_events(self, task_id: str, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return self._execute(
            "event listing", self._client.table("research_task_events").select("*").eq("task_id", task_id)
            .gt("sequence_no", after).order("sequence_no").limit(min(max(limit, 1), 500))
        )

    def claim_task(self, task_id: str, worker_id: str, *, resume: bool) -> bool:
        try:
            response = self._client.rpc("claim_research_task", {
                "p_task_id": task_id, "p_worker_id": worker_id, "p_resume": resume,
            }).execute()
            data = response.data
            if isinstance(data, list):
                data = data[0] if data else False
            if isinstance(data, dict):
                data = next(iter(data.values()), False)
            return data is True
        except Exception as exc:
            raise PersistenceError(f"Supabase task claim failed ({exc.__class__.__name__})") from exc

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        try:
            data = self._client.rpc("heartbeat_research_task", {
                "p_task_id": task_id, "p_worker_id": worker_id,
            }).execute().data
            if isinstance(data, list): data = data[0] if data else False
            if isinstance(data, dict): data = next(iter(data.values()), False)
            return data is True
        except Exception as exc:
            raise PersistenceError(f"Supabase worker heartbeat failed ({exc.__class__.__name__})") from exc

    def release_task(self, task_id: str, worker_id: str) -> None:
        try:
            self._client.rpc("release_research_task", {"p_task_id": task_id, "p_worker_id": worker_id}).execute()
        except Exception as exc:
            raise PersistenceError(f"Supabase worker release failed ({exc.__class__.__name__})") from exc

    def request_cancel(self, task_id: str) -> bool:
        try:
            data = self._client.rpc("request_research_cancellation", {"p_task_id": task_id}).execute().data
            if isinstance(data, list): data = data[0] if data else False
            if isinstance(data, dict): data = next(iter(data.values()), False)
            return data is True
        except Exception as exc:
            raise PersistenceError(f"Supabase cancellation request failed ({exc.__class__.__name__})") from exc
