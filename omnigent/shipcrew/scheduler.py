"""Background loop: sync session state onto cards, then start gated ready tasks.

State lives in the DB; every tick recomputes capacity, overlap and budget from
it, so a restart or a manual edit never leaves the loop with a stale view.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from omnigent.shipcrew.service import ShipcrewService

_logger = logging.getLogger(__name__)


class ShipcrewScheduler:
    """Periodic ``tick()`` driver.

    :param get_service: Returns the board service; called per tick so the
        store (and its schema migration) is only built once the loop runs.
    :param interval_s: Seconds between ticks.
    """

    def __init__(self, get_service: Callable[[], ShipcrewService], interval_s: float) -> None:
        self._get_service = get_service
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    async def tick(self) -> list[str]:
        """One pass: session sync, then gated starts. Returns started task ids."""
        service = await asyncio.to_thread(self._get_service)
        await service.sync_active()
        return await service.schedule_ready()

    def poke(self) -> None:
        """Run the next tick now (e.g. right after a card moved to ready)."""
        self._wake.set()

    async def _run(self) -> None:
        failing = False
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval_s)
            self._wake.clear()
            try:
                await self.tick()
            except Exception:
                # A failing tick must never kill the loop; the next one retries.
                # Log the traceback once per failure streak, not every tick.
                log = _logger.debug if failing else _logger.exception
                log("shipcrew scheduler tick failed")
                failing = True
            else:
                failing = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="shipcrew-scheduler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
