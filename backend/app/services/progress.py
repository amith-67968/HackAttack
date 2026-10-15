import asyncio
from collections import defaultdict
from typing import Any, AsyncIterator


class ProgressHub:
    """In-process progress fanout with short-lived replay for late SSE subscribers."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._next_id: dict[str, int] = defaultdict(int)

    async def publish(self, task_id: str, event_type: str, data: dict[str, Any], *, event_id: int | None = None) -> None:
        async with self._condition:
            events = self._events[task_id]
            self._next_id[task_id] = max(self._next_id[task_id] + 1, event_id or 0)
            events.append({"id": event_id or self._next_id[task_id], "event": event_type, "data": data})
            del events[:-500]
            self._condition.notify_all()

    def history(self, task_id: str) -> list[dict[str, Any]]:
        return list(self._events.get(task_id, []))

    async def stream(self, task_id: str, after: int = 0) -> AsyncIterator[dict[str, Any]]:
        cursor = after
        while True:
            async with self._condition:
                await self._condition.wait_for(
                    lambda: bool(self._events[task_id]) and self._events[task_id][-1]["id"] > cursor
                )
                pending = [event for event in self._events[task_id] if event["id"] > cursor]
            for event in pending:
                cursor = event["id"]
                yield event
                if event["event"] in {"task_completed", "task_error"}:
                    return


progress_hub = ProgressHub()
