"""Background loop: sync session state onto cards, then start gated ready tasks.

Each tick also imports finished planner runs, redeploys the preview of
missions whose ``main`` moved, ships missions whose tasks are all merged, and
(every ``sync_interval_s``) syncs GitHub issues.

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
        """One pass: session sync, plan import, PR loop, gated starts, ship, GitHub sync.

        Returns the started task ids.
        """
        service = await asyncio.to_thread(self._get_service)
        await service.sync_active()
        await service.planner.sync()
        # Before the starts: a merge this tick unblocks dependants right away.
        await service.advance_reviews()
        started = await service.schedule_ready()
        # After the merges above: a merge redeploys main (server-side targets),
        # the first merge publishes the mission's live URL.
        await asyncio.to_thread(service.deploy_target)
        await service.preview.tick()
        # After the merges above: the last merge of a mission ships it this tick.
        await service.ship.tick()
        # Last: gh calls are the slowest part, and each one is time-bounded.
        await service.github.tick()
        return started

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
                if failing:
                    _logger.debug("shipcrew scheduler tick still failing", exc_info=True)
                else:
                    _logger.exception("shipcrew scheduler tick failed")
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
