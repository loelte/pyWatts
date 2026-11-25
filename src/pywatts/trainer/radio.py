"""One scan or connection attempt at a time: BlueZ refuses a second one with "InProgress"."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

YIELD_TIMEOUT_S = 4.0  # longer than a sensor needs to hang up


class _Turn:
    def __init__(self, priority: bool) -> None:
        self.priority = priority
        self.task: asyncio.Task[Any] | None = None
        self.preempted = False


class Radio:
    """Shared by the trainer and the sensors of one BLE loop; used on that loop only.

    Connecting the trainer has priority: it cancels what a sensor is doing, which is repeated
    afterwards. So `op` must clean up when it is cancelled, and start its own timeouts.
    Never used for control commands or for hanging up: those must not wait.
    """

    def __init__(self, yield_timeout: float = YIELD_TIMEOUT_S) -> None:
        self._yield_timeout = yield_timeout
        self._turn: _Turn | None = None
        self._urgent = 0  # waiting with priority
        self._changed: asyncio.Future[None] | None = None

    async def run(self, op: Callable[[], Coroutine[Any, Any, T]], *, priority: bool = False) -> T:
        while True:
            turn = _Turn(priority)
            await self._take(turn)
            try:
                task = turn.task = asyncio.ensure_future(op())
                return await task
            except (Exception, asyncio.CancelledError):
                caller = asyncio.current_task()
                if not turn.preempted or (caller is not None and caller.cancelling()):
                    raise
                log.info("gave way to the trainer, trying again")
            finally:
                if self._turn is turn:  # else it was taken away
                    self._turn = None
                    self._notify()

    async def _take(self, turn: _Turn) -> None:
        if turn.priority:
            self._urgent += 1
        try:
            while (current := self._turn) is not None or (self._urgent and not turn.priority):
                if not turn.priority or current is None:
                    await self._wait(None)
                    continue
                if not current.priority and not current.preempted:
                    current.preempted = True
                    if current.task is not None:
                        current.task.cancel()
                # A priority turn that is still there was cancelled before, see FtmsTrainer.
                if not await self._wait(self._yield_timeout) and self._turn is current:
                    log.warning("a Bluetooth operation does not end, connecting anyway")
                    break
            self._turn = turn  # no await since the check
        finally:
            if turn.priority:
                self._urgent -= 1
                self._notify()

    async def _wait(self, timeout: float | None) -> bool:
        """Until something changed. False: timeout."""
        if self._changed is None:
            self._changed = asyncio.get_running_loop().create_future()
        done, _ = await asyncio.wait({self._changed}, timeout=timeout)
        return bool(done)

    def _notify(self) -> None:
        changed, self._changed = self._changed, None
        if changed is not None:
            changed.set_result(None)
