"""Simulated trainer for development and tests. No Qt, no bleak."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Callable

from pywatts.trainer.base import (
    ConnectionState,
    DeviceInfo,
    HeartRate,
    HeartRateEvent,
    PowerEvent,
    PowerReading,
    Telemetry,
    TrainerError,
    TrainerEvent,
)

log = logging.getLogger(__name__)

FAKE_DEVICE = DeviceInfo("Fake Trainer", "FA:KE:00:00:00:01", -50)
FAKE_STRAP = DeviceInfo("TICKR FAKE", "FA:KE:00:00:00:02", -60)
FAKE_METER = DeviceInfo("Fake Power Meter", "FA:KE:00:00:00:03", -55)
METER_FACTOR = 0.96  # it disagrees with the trainer, as real ones do

_POWER_TAU_S = 2.0
_OPEN_POWER_W = 600  # what the simulated rider does in open mode
_HR_TAU_S = 30.0
_HR_REST = 70.0


class FakeTrainer:
    """Emits one sample per simulated second; time_scale > 1 runs faster than real time."""

    def __init__(self, time_scale: float = 1.0, seed: int | None = None) -> None:
        if time_scale <= 0:
            raise ValueError("time_scale must be > 0")
        self._scale = time_scale
        self._rng = random.Random(seed)
        self._listeners: list[Callable[[TrainerEvent], None]] = []
        self._state = ConnectionState.DISCONNECTED
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._target = 0
        self._open = False
        self._power = 0.0
        self._cadence = 0.0
        self._hr = _HR_REST

    def add_listener(self, cb: Callable[[TrainerEvent], None]) -> None:
        self._listeners.append(cb)

    @property
    def pedalling(self) -> tuple[float, float]:
        """Power and cadence of the simulated rider."""
        return self._power, self._cadence

    async def scan(self, timeout: float = 5.0) -> list[DeviceInfo]:
        await asyncio.sleep(min(timeout, 0.5) / self._scale)
        return [FAKE_DEVICE]

    async def connect(self, addr: str) -> None:
        await self.disconnect()
        self._set_state(ConnectionState.CONNECTING)
        try:
            await asyncio.sleep(0.2 / self._scale)
        except asyncio.CancelledError:
            self._set_state(ConnectionState.DISCONNECTED)
            raise
        if self._state is not ConnectionState.CONNECTING:
            raise TrainerError("Connecting was cancelled")
        self._running = True
        self._set_state(ConnectionState.CONNECTED)
        self._task = asyncio.ensure_future(self._simulate())

    async def disconnect(self) -> None:
        task, self._task = self._task, None
        self._running = False
        self._target = 0
        self._open = False
        self._power = self._cadence = 0.0
        if task is not None:
            task.cancel()
            await asyncio.wait({task})
        self._set_state(ConnectionState.DISCONNECTED)

    async def set_target_power(self, w: int) -> None:
        self._require()
        self._target = max(0, min(2000, int(w)))
        self._open = False

    async def set_open_mode(self) -> None:
        self._require()
        self._open = True

    async def start(self) -> None:
        self._require()
        self._running = True

    async def pause(self) -> None:
        self._require()
        self._running = False

    async def stop(self) -> None:
        self._require()
        self._running = False
        self._target = 0
        self._open = False

    def _require(self) -> None:
        if self._state is not ConnectionState.CONNECTED:
            raise TrainerError("Trainer is not connected")

    async def _simulate(self) -> None:
        while True:
            await asyncio.sleep(1.0 / self._scale)
            self._emit(self._step())

    def _step(self) -> Telemetry:
        """Advance one simulated second."""
        wanted = _OPEN_POWER_W if self._open else self._target
        target = wanted if self._running else 0
        riding = target > 0
        self._power += (target - self._power) / _POWER_TAU_S
        self._cadence += ((90.0 if riding else 0.0) - self._cadence) / _POWER_TAU_S
        self._hr += (min(190.0, _HR_REST + 0.35 * self._power) - self._hr) / _HR_TAU_S
        power, cadence = self._power, self._cadence
        if riding:
            power += self._rng.gauss(0, 1 + 0.015 * target)
            cadence += self._rng.gauss(0, 1.0)
        power = max(0.0, power)
        return Telemetry(
            time.monotonic(),
            round(power),
            round(max(0.0, cadence) * 2) / 2,
            round(5.2 * power ** (1 / 3), 1),
            round(self._hr + self._rng.gauss(0, 0.5)),
        )

    def _set_state(self, state: ConnectionState) -> None:
        if state is not self._state:
            self._state = state
            self._emit(state)

    def _emit(self, event: TrainerEvent) -> None:
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:
                log.exception("trainer listener failed")


class FakeHrStrap:
    """Simulated strap: one reading per second, slowly wandering through all zones."""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._listeners: list[Callable[[HeartRateEvent], None]] = []
        self._state = ConnectionState.DISCONNECTED
        self._task: asyncio.Task[None] | None = None

    def add_listener(self, cb: Callable[[HeartRateEvent], None]) -> None:
        self._listeners.append(cb)

    async def scan(self, timeout: float = 5.0) -> list[DeviceInfo]:
        await asyncio.sleep(min(timeout, 0.5))
        return [FAKE_STRAP]

    async def connect(self, addr: str) -> None:
        await self.disconnect()
        self._set_state(ConnectionState.CONNECTED)
        self._task = asyncio.ensure_future(self._simulate())

    async def disconnect(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.wait({task})
        self._set_state(ConnectionState.DISCONNECTED)

    async def _simulate(self) -> None:
        start = time.monotonic()
        while True:
            now = time.monotonic()
            wave = 135 - 45 * math.cos((now - start) / 60)
            self._emit(HeartRate(now, round(wave + self._rng.gauss(0, 1)), 80))
            await asyncio.sleep(1)

    def _set_state(self, state: ConnectionState) -> None:
        if state is not self._state:
            self._state = state
            self._emit(state)

    def _emit(self, event: HeartRateEvent) -> None:
        for cb in list(self._listeners):
            cb(event)


class FakePowerMeter:
    """Simulated power meter on the bike of `rider` (power, cadence); one reading per second."""

    def __init__(
        self, rider: Callable[[], tuple[float, float]] = lambda: (200.0, 90.0), period: float = 1.0
    ) -> None:
        self._rider = rider
        self._period = period
        self._listeners: list[Callable[[PowerEvent], None]] = []
        self._state = ConnectionState.DISCONNECTED
        self._task: asyncio.Task[None] | None = None

    def add_listener(self, cb: Callable[[PowerEvent], None]) -> None:
        self._listeners.append(cb)

    async def scan(self, timeout: float = 5.0) -> list[DeviceInfo]:
        await asyncio.sleep(min(timeout, 0.5))
        return [FAKE_METER]

    async def connect(self, addr: str) -> None:
        await self.disconnect()
        self._set_state(ConnectionState.CONNECTED)
        self._task = asyncio.ensure_future(self._simulate())

    async def disconnect(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.wait({task})
        self._set_state(ConnectionState.DISCONNECTED)

    async def _simulate(self) -> None:
        while True:
            power, cadence = self._rider()
            watts = round(max(0.0, power) * METER_FACTOR)
            self._emit(PowerReading(time.monotonic(), watts, round(cadence), 70))
            await asyncio.sleep(self._period)

    def _set_state(self, state: ConnectionState) -> None:
        if state is not self._state:
            self._state = state
            self._emit(state)

    def _emit(self, event: PowerEvent) -> None:
        for cb in list(self._listeners):
            cb(event)
