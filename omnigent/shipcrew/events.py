"""In-process fan-out of board events to per-mission SSE subscribers."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from typing import Any

from omnigent.shipcrew.store import Mission, Task

# Slow consumers drop events past this backlog instead of growing memory; the
# board refetches on reconnect.
_QUEUE_MAX = 1000


class MissionEventBus:
    """Publish ``task.updated`` / ``task.deleted`` / ``mission.updated`` events per mission.

    Publishing is thread-safe: store work runs in worker threads, so events are
    handed to each subscriber's loop with ``call_soon_threadsafe``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[Any]]]] = {}

    def publish(self, mission_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs.get(mission_id, ()))
        for loop, queue in subs:
            with contextlib.suppress(RuntimeError):  # loop already closed
                loop.call_soon_threadsafe(_put_nowait, queue, event)

    def task_updated(self, task: Task) -> None:
        self.publish(task.mission_id, {"type": "task.updated", "task": task.to_api()})

    def mission_updated(self, mission: Mission) -> None:
        self.publish(mission.id, {"type": "mission.updated", "mission": mission.to_api()})

    def task_deleted(self, mission_id: str, task_id: str) -> None:
        self.publish(mission_id, {"type": "task.deleted", "id": task_id})

    def subscriber_count(self, mission_id: str) -> int:
        with self._lock:
            return len(self._subs.get(mission_id, ()))

    @contextlib.asynccontextmanager
    async def subscribe(self, mission_id: str) -> AsyncIterator[asyncio.Queue[Any]]:
        """Register a queue for ``mission_id`` for the duration of the block."""
        entry = (asyncio.get_running_loop(), asyncio.Queue[Any](maxsize=_QUEUE_MAX))
        with self._lock:
            self._subs.setdefault(mission_id, set()).add(entry)
        try:
            yield entry[1]
        finally:
            with self._lock:
                subs = self._subs.get(mission_id)
                if subs is not None:
                    subs.discard(entry)
                    if not subs:
                        del self._subs[mission_id]


def _put_nowait(queue: asyncio.Queue[Any], event: dict[str, Any]) -> None:
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(event)
