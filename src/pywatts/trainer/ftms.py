"""FTMS (Fitness Machine Service) over BLE: pure parsers and the bleak-based FtmsTrainer.

No COM setup is needed on Windows: bleak 3.1 accepts an uninitialised thread and WinRT then joins
the implicit MTA, even while the GUI thread is STA. Just keep the loop off the GUI thread.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakBluetoothNotAvailableError, BleakDBusError

from pywatts import platform
from pywatts.trainer.base import ConnectionState, DeviceInfo, Telemetry, TrainerError, TrainerEvent
from pywatts.trainer.cycling_power import (
    CP_MEASUREMENT,
    CP_SERVICE,
    CrankCadence,
    parse_power_measurement,
)
from pywatts.trainer.radio import Radio

log = logging.getLogger(__name__)


def ftms_uuid(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


FTMS_SERVICE = ftms_uuid(0x1826)
FEATURES = ftms_uuid(0x2ACC)
BIKE_DATA = ftms_uuid(0x2AD2)
POWER_RANGE = ftms_uuid(0x2AD8)
CONTROL_POINT = ftms_uuid(0x2AD9)
MACHINE_STATUS = ftms_uuid(0x2ADA)
HR_SERVICE = ftms_uuid(0x180D)
HR_MEASUREMENT = ftms_uuid(0x2A37)

OP_REQUEST_CONTROL = 0x00
OP_SET_TARGET_POWER = 0x05
OP_START = 0x07
OP_STOP_PAUSE = 0x08
OP_SET_SIMULATION = 0x11
OP_RESPONSE = 0x80

RESULT_SUCCESS = 0x01
RESULT_NOT_SUPPORTED = 0x02
RESULT_NOT_PERMITTED = 0x05

STATUS_RESET = 0x01
STATUS_CONTROL_LOST = 0xFF

MAX_TARGET_W = 2000

# Open mode = simulated flat road: resistance follows the speed, as on a bike outside.
# Wind 0 (s16, 0.001 m/s), grade 0 (s16, 0.01 %), Crr 0.004 (u8, 0.0001), Cw 0.51 (u8, 0.01 kg/m).
OPEN_MODE_PARAMS = struct.pack("<hhBB", 0, 0, 40, 51)

_OP_NAMES = {
    OP_REQUEST_CONTROL: "request control",
    OP_SET_TARGET_POWER: "set target power",
    OP_START: "start",
    OP_STOP_PAUSE: "stop/pause",
    OP_SET_SIMULATION: "open mode",
}
_RESULT_NAMES = {
    RESULT_NOT_SUPPORTED: "operation not supported",
    0x03: "invalid parameter",
    0x04: "operation failed",
    RESULT_NOT_PERMITTED: "control not permitted (is another app controlling the trainer?)",
}


# --- parsers -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class BikeData:
    """Fields of one notification; None = absent (or cut off)."""

    more_data: bool = False
    speed_kph: float | None = None
    avg_speed_kph: float | None = None
    cadence_rpm: float | None = None
    avg_cadence_rpm: float | None = None
    distance_m: int | None = None
    resistance: int | None = None
    power_w: int | None = None
    avg_power_w: int | None = None
    energy_kcal: int | None = None
    energy_per_hour: int | None = None
    energy_per_min: int | None = None
    hr_bpm: int | None = None
    met: float | None = None
    elapsed_s: int | None = None
    remaining_s: int | None = None


# (flag bit, size, signed, scale, field); bit 0 is inverted (set = no speed).
_BIKE_FIELDS: tuple[tuple[int, int, bool, float, str], ...] = (
    (0, 2, False, 0.01, "speed_kph"),
    (1, 2, False, 0.01, "avg_speed_kph"),
    (2, 2, False, 0.5, "cadence_rpm"),
    (3, 2, False, 0.5, "avg_cadence_rpm"),
    (4, 3, False, 1, "distance_m"),
    (5, 2, True, 1, "resistance"),
    (6, 2, True, 1, "power_w"),
    (7, 2, True, 1, "avg_power_w"),
    (8, 2, False, 1, "energy_kcal"),
    (8, 2, False, 1, "energy_per_hour"),
    (8, 1, False, 1, "energy_per_min"),
    (9, 1, False, 1, "hr_bpm"),
    (10, 1, False, 0.1, "met"),
    (11, 2, False, 1, "elapsed_s"),
    (12, 2, False, 1, "remaining_s"),
)


def parse_bike_data(data: bytes) -> BikeData:
    """Parse 0x2AD2. Truncated packets yield the complete fields before the cut."""
    if len(data) < 2:
        return BikeData(more_data=True)
    flags = int.from_bytes(data[:2], "little") ^ 1  # bit 0 now reads "speed present"
    values: dict[str, Any] = {}
    pos = 2
    for bit, size, signed, scale, name in _BIKE_FIELDS:
        if not flags & (1 << bit):
            continue
        if pos + size > len(data):
            break
        raw = int.from_bytes(data[pos : pos + size], "little", signed=signed)
        values[name] = raw if scale == 1 else round(raw * scale, 2)
        pos += size
    return BikeData(more_data=not flags & 1, **values)


def parse_hr_measurement(data: bytes) -> int | None:
    """Parse 0x2A37. None if truncated, zero, or the sensor reports no skin contact."""
    if len(data) < 2:
        return None
    flags = data[0]
    if flags & 0x06 == 0x04:  # contact supported but not detected
        return None
    if flags & 0x01:
        if len(data) < 3:
            return None
        return int.from_bytes(data[1:3], "little") or None
    return data[1] or None


@dataclass(frozen=True)
class Features:
    machine: int
    target: int

    @property
    def cadence(self) -> bool:
        return bool(self.machine & (1 << 1))

    @property
    def heart_rate(self) -> bool:
        return bool(self.machine & (1 << 10))

    @property
    def power_measurement(self) -> bool:
        return bool(self.machine & (1 << 14))

    @property
    def power_target(self) -> bool:
        return bool(self.target & (1 << 3))

    @property
    def simulation(self) -> bool:
        """Needed for open mode."""
        return bool(self.target & (1 << 13))


def parse_features(data: bytes) -> Features | None:
    """Parse 0x2ACC."""
    if len(data) < 4:
        return None
    target = struct.unpack_from("<I", data, 4)[0] if len(data) >= 8 else 0
    return Features(struct.unpack_from("<I", data)[0], target)


def parse_power_range(data: bytes) -> tuple[int, int] | None:
    """Parse 0x2AD8 to (min, max). None if truncated or nonsensical."""
    if len(data) < 4:
        return None
    lo, hi = struct.unpack_from("<hh", data)
    return (lo, hi) if hi > 0 and lo < hi else None


@dataclass(frozen=True)
class MachineStatus:
    op: int
    target_power_w: int | None = None

    @property
    def control_lost(self) -> bool:
        return self.op in (STATUS_RESET, STATUS_CONTROL_LOST)


def parse_status(data: bytes) -> MachineStatus | None:
    """Parse 0x2ADA."""
    if not data:
        return None
    if data[0] == 0x08 and len(data) >= 3:
        return MachineStatus(data[0], struct.unpack_from("<h", data, 1)[0])
    return MachineStatus(data[0])


@dataclass(frozen=True)
class ControlResponse:
    op: int
    result: int


def parse_control_response(data: bytes) -> ControlResponse | None:
    """Parse a 0x2AD9 indication."""
    if len(data) < 3 or data[0] != OP_RESPONSE:
        return None
    return ControlResponse(data[1], data[2])


# --- trainer -----------------------------------------------------------------------------------


class _ConnectionLost(TrainerError):
    pass


class _Session:
    """State of one physical connection; callbacks of older sessions are ignored."""

    def __init__(self) -> None:
        self.client: Any = None
        self.lost = False
        self.pending: tuple[int, asyncio.Future[int]] | None = None
        self.power_range: tuple[int, int] | None = None
        self.bike: dict[str, Any] = {}
        self.hr: tuple[float, int] | None = None
        # from the Cycling Power service: time, power, cadence
        self.cp: tuple[float, int, float | None] | None = None
        self.crank = CrankCadence()
        self.unsupported = False  # last executed command was "not supported"
        self.halted = False  # last load-changing write released the load

    def fail(self) -> None:
        self.lost = True
        if self.pending is not None and not self.pending[1].done():
            self.pending[1].set_exception(_ConnectionLost("Connection to the trainer was lost"))


_BIKE_NAMES = [f.name for f in fields(BikeData) if f.name != "more_data"]
_FRESH_S = 5.0


class FtmsTrainer:
    """Trainer implementation. `client_factory`/`scanner` mirror BleakClient/BleakScanner."""

    def __init__(
        self,
        client_factory: Callable[..., Any] = BleakClient,
        scanner: Any = BleakScanner,
        *,
        radio: Radio | None = None,
        clock: Callable[[], float] = time.monotonic,
        find_timeout: float = 10.0,
        connect_timeout: float = 45.0,
        command_timeout: float = 5.0,
        close_timeout: float = 3.0,
        resend_interval: float = 5.0,
        backoff: tuple[float, float] = (1.0, 30.0),
    ) -> None:
        self._client_factory = client_factory
        self._scanner = scanner
        self._radio = radio or Radio()
        self._clock = clock
        self._find_timeout = find_timeout
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._close_timeout = close_timeout
        self._resend_interval = resend_interval
        self._backoff = backoff

        self._listeners: list[Callable[[TrainerEvent], None]] = []
        self._state = ConnectionState.DISCONNECTED
        self._addr = ""
        self._session: _Session | None = None
        self._lock = asyncio.Lock()
        self._running = False
        self._target: int | None = None
        self._open_mode = False  # open mode wanted instead of _target
        self._target_deferred = False  # set while paused, to be sent by start()
        self._start_pending = False  # start failed while wanted; resend loop retries
        self._connect_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._resend_task: asyncio.Task[None] | None = None
        self._recover_task: asyncio.Task[None] | None = None

    # public API

    def add_listener(self, cb: Callable[[TrainerEvent], None]) -> None:
        self._listeners.append(cb)

    async def scan(self, timeout: float = 5.0) -> list[DeviceInfo]:
        try:
            found = await self._radio.run(
                lambda: self._scanner.discover(timeout=timeout, return_adv=True)
            )
        except (Exception, asyncio.CancelledError) as e:
            reraise_own_cancel(e)
            raise TrainerError(f"Bluetooth scan failed: {describe_error(e)}") from e
        devices = []
        for dev, adv in found.values():
            uuids = {u.lower() for u in (*adv.service_uuids, *adv.service_data)}
            # Some trainers advertise only their power measurement; so do power meters.
            if FTMS_SERVICE in uuids or CP_SERVICE in uuids:
                name = adv.local_name or dev.name or "Unknown trainer"
                devices.append(DeviceInfo(name, dev.address, adv.rssi, FTMS_SERVICE in uuids))
        return sorted(devices, key=lambda d: (not d.likely, -d.rssi))

    async def connect(self, addr: str) -> None:
        await self.disconnect()
        self._addr = addr
        self._set_state(ConnectionState.CONNECTING)
        self._running = True  # connect itself sends start; no target is set yet
        task = self._connect_task = asyncio.ensure_future(self._establish(addr))
        try:
            await task
        except BaseException as e:
            if self._connect_task is task:  # else disconnect() already took over
                self._running = False
                self._set_state(ConnectionState.DISCONNECTED)
            if isinstance(e, asyncio.CancelledError):
                current = asyncio.current_task()
                if current is None or not current.cancelling():
                    raise TrainerError("Connecting was cancelled") from None
            raise
        finally:
            if self._connect_task is task:
                self._connect_task = None

    async def disconnect(self) -> None:
        tasks = [
            t
            for t in (
                self._connect_task,
                self._reconnect_task,
                self._resend_task,
                self._recover_task,
            )
            if t is not None and not t.done()
        ]
        self._connect_task = self._reconnect_task = None
        self._resend_task = self._recover_task = None
        session, self._session = self._session, None
        self._running = False
        self._target = None
        self._open_mode = False
        self._target_deferred = self._start_pending = False
        try:
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=self._close_timeout + 1)
            if session is not None:
                await self._release(session)
                await self._close(session)
        finally:
            self._set_state(ConnectionState.DISCONNECTED)

    async def set_target_power(self, w: int) -> None:
        self._require_session()
        self._target = max(0, min(MAX_TARGET_W, int(w)))
        self._open_mode = False
        await self._target_changed()

    async def set_open_mode(self) -> None:
        """Leave ERG until the next set_target_power; same pause/reconnect rules as a target."""
        self._require_session()
        self._open_mode = True
        await self._target_changed()

    async def _target_changed(self) -> None:
        if not self._running:
            self._target_deferred = True
            return
        await self._while_connected(self._send_target)

    async def start(self) -> None:
        self._require_session()

        async def run(s: _Session) -> None:
            async with self._lock:
                before, self._running = self._running, True
                try:
                    await self._execute(s, OP_START, tolerate_unsupported=True)
                except TrainerError as e:
                    # Still wanted: the ride goes on, so the resend loop retries.
                    self._start_pending = self._running and not isinstance(e, _ConnectionLost)
                    raise
                except BaseException:
                    self._running = before
                    raise
                self._start_pending = False
                s.halted = False
                if self._target_deferred:
                    await self._write_target(s)

        if self._session is None:  # reconnecting: applied once the link is back
            self._running = True
        await self._while_connected(run)

    async def pause(self) -> None:
        self._require_session()
        self._start_pending = False
        self._running = False  # before waiting for the lock, so no resend slips in behind
        await self._halt(b"\x02")

    async def stop(self) -> None:
        self._require_session()
        self._running = False
        self._target = None
        self._open_mode = False
        self._target_deferred = self._start_pending = False
        await self._halt(b"\x01")

    async def read_features(self) -> Features | None:
        """None if the trainer does not expose a (valid) feature characteristic."""
        session = self._require_session()
        if session is None:
            return None
        return await self._features(session)

    async def _features(self, s: _Session) -> Features | None:
        data = await self._read_optional(s, FEATURES)
        return None if data is None else parse_features(data)

    async def request_control(self) -> None:
        """Explicit request; connect() and the recovery paths already do this."""
        self._require_session()

        async def run(s: _Session) -> None:
            async with self._lock:
                await self._execute(s, OP_REQUEST_CONTROL)

        await self._while_connected(run)

    # connection

    def _require_session(self) -> _Session | None:
        """Session, or None while reconnecting (the wanted state is applied afterwards)."""
        if self._session is None and not self._reconnecting:
            raise TrainerError("Trainer is not connected")
        return self._session

    @property
    def _reconnecting(self) -> bool:
        return self._state is ConnectionState.RECONNECTING

    async def _while_connected(self, fn: Callable[[_Session], Awaitable[None]]) -> None:
        """Run fn; no-op if the link is (or went) down and reconnecting will catch up."""
        session = self._session
        if session is None:
            return
        try:
            await fn(session)
        except _ConnectionLost:
            if not self._reconnecting:
                raise

    async def _halt(self, param: bytes) -> None:
        async def run(s: _Session) -> None:
            async with self._lock:
                await self._halt_locked(s, param)

        await self._while_connected(run)

    async def _halt_locked(self, s: _Session, param: bytes) -> None:
        """Stop/pause with one retry; a trainer refusing both gets its minimum target instead.

        Caller holds the lock. Returning normally means the load was released.
        """
        error: TrainerError | None = None
        for _ in range(2):
            try:
                await self._execute(s, OP_STOP_PAUSE, param)
                s.halted = True
                return
            except _ConnectionLost:
                raise
            except TrainerError as e:
                error = e
                log.warning("halt failed: %s", e)
                if s.unsupported:
                    break
        try:
            await self._write_min_target(s)
        except _ConnectionLost:
            raise
        except TrainerError:
            raise TrainerError(f"Trainer may still hold resistance! {error}") from error
        s.halted = True

    async def _write_min_target(self, s: _Session) -> None:
        w = s.power_range[0] if s.power_range is not None else 0
        w = max(0, min(MAX_TARGET_W, w))
        await self._execute(s, OP_SET_TARGET_POWER, struct.pack("<h", w))

    async def _write_open_mode(self, s: _Session) -> None:
        """Caller holds the lock. A refusal must not leave the previous ERG load on."""
        s.halted = False
        try:
            await self._execute(s, OP_SET_SIMULATION, OPEN_MODE_PARAMS)
        except _ConnectionLost:
            raise
        except TrainerError as error:
            if not self._running:
                raise  # paused meanwhile: the halt is waiting for the lock
            try:
                await self._write_min_target(s)
            except _ConnectionLost:
                raise
            except TrainerError:
                raise TrainerError(f"Trainer may still hold resistance! {error}") from error
            raise TrainerError(f"No open mode, resistance set to minimum. {error}") from error

    async def _release(self, s: _Session) -> None:
        """Best effort before hanging up: never leave a trainer holding an ERG load."""
        if s.lost or s.halted:
            return
        try:
            async with asyncio.timeout(min(1.0, self._close_timeout / 2)), self._lock:
                await self._halt_locked(s, b"\x01")
        except (Exception, asyncio.CancelledError) as e:
            reraise_own_cancel(e)
            log.warning("could not stop the trainer before disconnecting: %s", describe_error(e))

    async def _establish(self, addr: str) -> None:
        """Open a connection and make it current. Raises TrainerError with all cleaned up."""
        session = _Session()

        async def attempt() -> None:
            async with asyncio.timeout(self._connect_timeout):
                await self._open(session, addr)

        try:
            await self._radio.run(attempt, priority=True)
            if session.lost:
                raise _ConnectionLost("Connection to the trainer was lost")
        except BaseException as e:
            await self._close(session)
            task = asyncio.current_task()
            if isinstance(e, asyncio.CancelledError) and not (task and task.cancelling()):
                # bleak's WinRT futures raise this when Windows aborts an operation.
                raise TrainerError("The system aborted the connection attempt") from e
            if isinstance(e, TrainerError) or not isinstance(e, Exception):
                raise
            if isinstance(e, TimeoutError):
                raise TrainerError("Connecting to the trainer timed out") from e
            raise TrainerError(f"Could not connect to the trainer: {describe_error(e)}") from e
        # No await from here on: a drop is either seen above or handled as RECONNECTING.
        self._session = session
        self._set_state(ConnectionState.CONNECTED)
        self._resend_task = asyncio.ensure_future(self._resend_loop(session))

    async def _open(self, s: _Session, addr: str) -> None:
        device = await find_device(self._scanner, addr, self._find_timeout)
        if device is None:
            raise TrainerError("Trainer not found. Is it powered on and not in use elsewhere?")
        s.client = self._client_factory(device, disconnected_callback=lambda _c: self._on_drop(s))
        await s.client.connect()
        if s.client.services.get_service(FTMS_SERVICE) is None:
            raise TrainerError("This device is not an FTMS trainer")
        await s.client.start_notify(CONTROL_POINT, lambda _c, d: self._on_control(s, d))
        await s.client.start_notify(BIKE_DATA, lambda _c, d: self._on_bike(s, d))
        await self._notify_optional(s, MACHINE_STATUS, lambda _c, d: self._on_status(s, d))
        if s.client.services.get_service(HR_SERVICE) is not None:
            await self._notify_optional(s, HR_MEASUREMENT, lambda _c, d: self._on_hr(s, d))
        # Only a hint: some trainers announce less than they can, so nothing is refused.
        features = await self._features(s)
        log.info("trainer features: %s", features)
        complete = features is not None and features.power_measurement and features.cadence
        if not complete and s.client.services.get_service(CP_SERVICE) is not None:
            await self._notify_optional(s, CP_MEASUREMENT, lambda _c, d: self._on_power(s, d))
        data = await self._read_optional(s, POWER_RANGE)
        s.power_range = None if data is None else parse_power_range(data)
        async with self._lock:
            await self._execute(s, OP_REQUEST_CONTROL)
            await self._apply_wanted(s)

    async def _notify_optional(self, s: _Session, char: str, cb: Callable[..., None]) -> None:
        try:
            await s.client.start_notify(char, cb)
        except (Exception, asyncio.CancelledError) as e:
            reraise_own_cancel(e)
            log.info("no notifications on %s: %s", char, describe_error(e))

    async def _read_optional(self, s: _Session, char: str) -> bytes | None:
        try:
            async with asyncio.timeout(self._command_timeout):
                return bytes(await s.client.read_gatt_char(char))
        except (Exception, asyncio.CancelledError) as e:
            reraise_own_cancel(e)
            log.info("cannot read %s: %s", char, describe_error(e))
            return None

    async def _close(self, s: _Session) -> None:
        s.fail()  # also makes the disconnect callback bleak synthesizes a no-op
        if s.client is None:
            return
        try:
            async with asyncio.timeout(self._close_timeout):
                await s.client.disconnect()
        except (Exception, asyncio.CancelledError) as e:
            reraise_own_cancel(e)
            log.info("disconnect failed: %s", describe_error(e))

    def _on_drop(self, s: _Session) -> None:
        if s.lost:
            return
        s.fail()
        if s is not self._session:
            return  # still connecting; _establish notices
        self._session = None
        for t in (self._resend_task, self._recover_task):
            if t is not None:
                t.cancel()
        self._resend_task = self._recover_task = None
        self._set_state(ConnectionState.RECONNECTING)
        # A listener may already have torn us down; only then is the state different.
        if self._reconnecting:
            self._reconnect_task = asyncio.ensure_future(self._reconnect(s))

    async def _reconnect(self, old: _Session) -> None:
        await self._close(old)
        delay, limit = self._backoff
        while True:
            await asyncio.sleep(delay)
            try:
                await self._establish(self._addr)
                return
            except TrainerError as e:
                log.info("reconnect failed: %s", e)
            delay = min(delay * 2, limit)

    # control

    async def _command(self, s: _Session, op: int, param: bytes = b"") -> int:
        """One write + indication. Caller holds the lock. Returns the result code."""
        if s.lost:
            raise _ConnectionLost("Connection to the trainer was lost")
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        s.pending = (op, future)
        try:
            async with asyncio.timeout(self._command_timeout):
                await s.client.write_gatt_char(CONTROL_POINT, bytes([op]) + param, response=True)
                return await future
        except TimeoutError as e:
            raise TrainerError(f"Trainer did not respond to '{_OP_NAMES[op]}'") from e
        except TrainerError:
            raise
        except (Exception, asyncio.CancelledError) as e:
            reraise_own_cancel(e)
            kind = _ConnectionLost if s.lost else TrainerError
            raise kind(f"Trainer command '{_OP_NAMES[op]}' failed: {describe_error(e)}") from e
        finally:
            s.pending = None
            if future.done() and not future.cancelled():
                future.exception()  # mark retrieved when the write failed first

    async def _execute(
        self, s: _Session, op: int, param: bytes = b"", *, tolerate_unsupported: bool = False
    ) -> None:
        """Command with permission recovery. Caller holds the lock."""
        result = await self._command(s, op, param)
        if result == RESULT_NOT_PERMITTED:
            granted = await self._command(s, OP_REQUEST_CONTROL)
            if granted != RESULT_SUCCESS:
                raise _rejected(OP_REQUEST_CONTROL, granted)
            result = await self._command(s, op, param)
        s.unsupported = result == RESULT_NOT_SUPPORTED
        if result == RESULT_SUCCESS:
            return
        if tolerate_unsupported and result == RESULT_NOT_SUPPORTED:
            log.info("trainer does not support '%s', ignored", _OP_NAMES[op])
            return
        raise _rejected(op, result)

    async def _write_target(self, s: _Session) -> None:
        """Caller holds the lock. Reads the target late so the newest value always wins."""
        if not self._running:
            return
        if self._open_mode:
            await self._write_open_mode(s)
            self._target_deferred = False
            return
        if self._target is None:
            return
        w = self._target
        if s.power_range is not None:
            w = max(s.power_range[0], min(s.power_range[1], w))
        w = max(0, min(MAX_TARGET_W, w))
        s.halted = False
        await self._execute(s, OP_SET_TARGET_POWER, struct.pack("<h", w))
        self._target_deferred = False

    async def _apply_wanted(self, s: _Session) -> None:
        """Start + target if wanted, else halt: the trainer may still hold the old load.

        Loops because start/pause/stop may arrive meanwhile.
        """
        started: bool | None = None
        while self._running is not started:
            if self._running:
                await self._execute(s, OP_START, tolerate_unsupported=True)
                started = True
                self._start_pending = False
                s.halted = False
                try:
                    await self._write_target(s)
                except _ConnectionLost:
                    raise
                except TrainerError as e:
                    log.warning("could not restore target: %s", e)
            else:
                param = b"\x01" if self._target is None and not self._open_mode else b"\x02"
                started = False
                try:
                    await self._halt_locked(s, param)
                except _ConnectionLost:
                    raise
                except TrainerError as e:  # e.g. already stopped; must not prevent reconnecting
                    log.warning("could not halt after reconnect: %s", e)

    async def _send_target(self, s: _Session) -> None:
        async with self._lock:
            await self._write_target(s)

    async def _resend_loop(self, s: _Session) -> None:
        while True:
            await asyncio.sleep(self._resend_interval)
            try:
                async with self._lock:
                    if self._start_pending and self._running:
                        await self._execute(s, OP_START, tolerate_unsupported=True)
                        self._start_pending = False
                    await self._write_target(s)
            except TrainerError as e:
                log.warning("resending target failed: %s", e)

    async def _recover_control(self, s: _Session, restart: bool) -> None:
        try:
            async with self._lock:
                await self._execute(s, OP_REQUEST_CONTROL)
                if restart and self._running:
                    await self._execute(s, OP_START, tolerate_unsupported=True)
                await self._write_target(s)
        except TrainerError as e:
            log.warning("could not regain control: %s", e)

    # callbacks (on the loop thread); must never raise

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

    def _on_control(self, s: _Session, data: bytearray) -> None:
        response = parse_control_response(bytes(data))
        if response is None or s.pending is None:
            return
        op, future = s.pending
        if response.op == op and not future.done():
            future.set_result(response.result)

    def _on_status(self, s: _Session, data: bytearray) -> None:
        status = parse_status(bytes(data))
        if status is None or not status.control_lost or s is not self._session:
            return
        if self._recover_task is None or self._recover_task.done():
            self._recover_task = asyncio.ensure_future(
                self._recover_control(s, restart=status.op == STATUS_RESET)
            )

    def _on_hr(self, s: _Session, data: bytearray) -> None:
        hr = parse_hr_measurement(bytes(data))
        s.hr = None if hr is None else (self._clock(), hr)

    def _on_power(self, s: _Session, data: bytearray) -> None:
        measured = parse_power_measurement(bytes(data))
        if measured.power_w is not None:
            now = self._clock()
            s.cp = (now, measured.power_w, s.crank.update(measured, now))

    def _on_bike(self, s: _Session, data: bytearray) -> None:
        if s.lost:
            return
        parsed = parse_bike_data(bytes(data))
        new = {n: v for n in _BIKE_NAMES if (v := getattr(parsed, n)) is not None}
        if not new:
            return
        s.bike.update(new)
        now = self._clock()
        hr = s.bike.get("hr_bpm") or None  # 0 = no sensor
        if s.hr is not None and now - s.hr[0] <= _FRESH_S:
            hr = s.hr[1]
        power, cadence = s.bike.get("power_w"), s.bike.get("cadence_rpm")
        if s.cp is not None and now - s.cp[0] <= _FRESH_S:
            power = s.cp[1] if power is None else power
            cadence = s.cp[2] if cadence is None else cadence
        self._emit(Telemetry(now, power, cadence, s.bike.get("speed_kph"), hr))


def _rejected(op: int, result: int) -> TrainerError:
    reason = _RESULT_NAMES.get(result, f"error code 0x{result:02x}")
    if op == OP_SET_TARGET_POWER and result == RESULT_NOT_SUPPORTED:
        reason += " (it offers no ERG mode over FTMS)"
    return TrainerError(f"Trainer rejected '{_OP_NAMES[op]}': {reason}")


def reraise_own_cancel(e: BaseException) -> None:
    """bleak's WinRT futures raise CancelledError when Windows aborts an operation; only a
    cancellation of our own task may propagate."""
    if isinstance(e, asyncio.CancelledError):
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise e


async def find_device(scanner: Any, addr: str, timeout: float) -> Any:
    """None if it is not there. The caller must not be connected to it."""
    device = await scanner.find_device_by_address(addr, timeout=timeout)
    if device is None and await platform.release_bluetooth_link(addr):
        # It was still linked, e.g. after a crash, and so did not advertise.
        device = await scanner.find_device_by_address(addr, timeout=timeout)
    return device


def describe_error(e: BaseException) -> str:
    if isinstance(e, BleakBluetoothNotAvailableError):
        return f"{e.args[0]} Is Bluetooth switched on?"
    if isinstance(e, BleakDBusError):
        if e.dbus_error == "org.freedesktop.DBus.Error.ServiceUnknown":
            return "The Bluetooth service is not running. Is the package bluez installed?"
        if e.dbus_error == "org.bluez.Error.NotReady":
            return "Bluetooth is switched off."
    return str(e) or type(e).__name__
