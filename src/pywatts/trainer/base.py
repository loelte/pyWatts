"""Trainer abstraction. No Qt, no bleak."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"  # control granted, ready for set_target_power
    RECONNECTING = "reconnecting"  # unexpected drop; impl retries until disconnect()


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    addr: str
    rssi: int
    likely: bool = True  # False = may not be what was looked for


@dataclass(frozen=True)
class Telemetry:
    """t = time.monotonic(). None = the trainer does not report it."""

    t: float
    power_w: int | None
    cadence_rpm: float | None
    speed_kph: float | None
    hr_bpm: int | None = None


@dataclass(frozen=True)
class HeartRate:
    """From a strap. bpm None = no skin contact; battery in percent."""

    t: float  # time.monotonic()
    bpm: int | None
    battery: int | None = None


@dataclass(frozen=True)
class PowerReading:
    """From a power meter. cadence_rpm None = not reported; battery in percent."""

    t: float  # time.monotonic()
    watts: int
    cadence_rpm: float | None
    battery: int | None = None


TrainerEvent = Telemetry | ConnectionState
HeartRateEvent = HeartRate | ConnectionState
PowerEvent = PowerReading | ConnectionState


class TrainerError(Exception):
    """Connect/control failure with a user-readable message."""


class Trainer(Protocol):
    """All methods run on the BLE loop. Listeners are called on that loop's thread."""

    async def scan(self, timeout: float = 5.0) -> list[DeviceInfo]: ...
    async def connect(self, addr: str) -> None: ...
    async def disconnect(self) -> None: ...
    async def set_target_power(self, w: int) -> None: ...
    async def set_open_mode(self) -> None: ...  # no ERG, until the next set_target_power
    async def start(self) -> None: ...
    async def pause(self) -> None: ...
    async def stop(self) -> None: ...
    def add_listener(self, cb: Callable[[TrainerEvent], None]) -> None: ...


class HeartRateMonitor(Protocol):
    """Same threading rules as Trainer. Reconnects by itself until disconnect()."""

    async def scan(self, timeout: float = 5.0) -> list[DeviceInfo]: ...
    async def connect(self, addr: str) -> None: ...
    async def disconnect(self) -> None: ...
    def add_listener(self, cb: Callable[[HeartRateEvent], None]) -> None: ...


class PowerMeterMonitor(Protocol):
    """Same rules as HeartRateMonitor."""

    async def scan(self, timeout: float = 5.0) -> list[DeviceInfo]: ...
    async def connect(self, addr: str) -> None: ...
    async def disconnect(self) -> None: ...
    def add_listener(self, cb: Callable[[PowerEvent], None]) -> None: ...
