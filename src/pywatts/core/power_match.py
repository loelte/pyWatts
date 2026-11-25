"""Shifts the trainer's target so that a power meter reads the planned power. Pure Python."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from pywatts.core.runner import TrainerControl

SETTLE_S = 10.0  # after a target change both readings are still moving
WINDOW_S = 10.0  # averaged
STEP_S = 5.0
STEP_W = 5
DEAD_BAND_W = 2
MIN_POWER_W = 50  # below, the rider is coasting
MAX_SHARE = 0.2  # of the target
MAX_W = 50
LOST_S = 30.0  # meter silent for longer: back to the trainer's own power


class PowerMatch:
    """TrainerControl between Runner and bridge. While disabled it only passes commands on.

    The correction is the difference of the two averages, not a sum of errors: the trainer
    holds its own power at what it is told, so nothing can wind up.
    """

    def __init__(self, trainer: TrainerControl, clock: Callable[[], float]) -> None:
        self._trainer = trainer
        self._clock = clock
        self._enabled = False
        self._running = False
        self._open = False
        self._target = 0
        self._sent = 0
        self._offset = 0  # learnt; what is used depends on the target
        self._settled_at = 0.0
        self._stepped_at = 0.0
        self._heard_at: float | None = None  # last reading of the meter
        self._differences: deque[tuple[float, float]] = deque()  # time, trainer - meter

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, on: bool) -> None:
        if on == self._enabled:
            return
        self._enabled = on
        self._differences.clear()
        self._change(0)

    @property
    def offset_w(self) -> int:
        """Added to the current target."""
        if self._open or not self._enabled:
            return 0
        limit = int(min(MAX_W, MAX_SHARE * self._target))
        return max(-limit, min(limit, self._offset))

    # --- TrainerControl ---
    def set_target_power(self, w: int) -> None:
        if w != self._target or self._open:
            self._settle()
        self._target = w
        self._open = False
        self._send()

    def set_open_mode(self) -> None:
        self._open = True
        self._settle()
        self._trainer.set_open_mode()

    def start(self) -> None:
        self._running = True
        self._settle()
        self._trainer.start()

    def pause(self) -> None:
        self._running = False
        self._trainer.pause()

    def stop(self) -> None:
        self._running = False
        self._trainer.stop()

    # --- readings ---
    def update(self, trainer_w: int | None, meter_w: int | None) -> None:
        """Called about once per second with fresh readings; None = there is none."""
        now = self._clock()
        if meter_w is not None:
            self._heard_at = now
        if not (self._enabled and self._running) or self._open or self._target <= 0:
            return
        if self._heard_at is None or now - self._heard_at > LOST_S:
            self._differences.clear()
            self._step(0, now)
            return
        if trainer_w is None or meter_w is None or min(trainer_w, meter_w) <= MIN_POWER_W:
            self._differences.clear()
            return
        if now < self._settled_at:
            return
        self._differences.append((now, trainer_w - meter_w))
        while self._differences[0][0] < now - WINDOW_S:
            self._differences.popleft()
        if now - self._differences[0][0] >= WINDOW_S - 1.5:
            mean = sum(d for _, d in self._differences) / len(self._differences)
            limit = min(MAX_W, MAX_SHARE * self._target)
            self._step(round(max(-limit, min(limit, mean))), now)

    def _step(self, wanted: int, now: float) -> None:
        change = wanted - self._offset
        if now - self._stepped_at < STEP_S or not change:
            return
        if abs(change) < DEAD_BAND_W and wanted:
            return
        self._stepped_at = now
        self._change(self._offset + max(-STEP_W, min(STEP_W, change)))

    def _change(self, offset: int) -> None:
        self._offset = offset
        if self._running and not self._open and self._target + self.offset_w != self._sent:
            self._send()

    def _send(self) -> None:
        self._sent = self._target + self.offset_w
        self._trainer.set_target_power(self._sent)

    def _settle(self) -> None:
        self._settled_at = self._clock() + SETTLE_S
        self._differences.clear()
