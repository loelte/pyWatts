"""Heart rate straps over BLE (standard Heart Rate service; made for Wahoo TICKR / TRACKR)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from bleak import BleakClient, BleakScanner

from pywatts.trainer.base import (
    ConnectionState,
    DeviceInfo,
    HeartRate,
    HeartRateEvent,
    TrainerError,
)
from pywatts.trainer.ftms import (
    FTMS_SERVICE,
    HR_MEASUREMENT,
    HR_SERVICE,
    describe_error,
    ftms_uuid,
    parse_hr_measurement,
    reraise_own_cancel,
)

log = logging.getLogger(__name__)

BATTERY_LEVEL = ftms_uuid(0x2A19)
WAHOO_NAMES = ("TICKR", "TRACKR")


def is_wahoo(name: str) -> bool:
    upper = name.upper().removeprefix("WAHOO").strip()
    return upper.startswith(WAHOO_NAMES)


class HrStrap:
    """HeartRateMonitor implementation. `client_factory`/`scanner` mirror bleak."""

    def __init__(
        self,
        client_factory: Callable[..., Any] = BleakClient,
        scanner: Any = BleakScanner,
        *,
        clock: Callable[[], float] = time.monotonic,
        find_timeout: float = 10.0,
        connect_timeout: float = 30.0,
        close_timeout: float = 2.0,
        backoff: tuple[float, float] = (1.0, 30.0),
    ) -> None:
        self._client_factory = client_factory
        self._scanner = scanner
        self._clock = clock
        self._find_timeout = find_timeout
        self._connect_timeout = connect_timeout
        self._close_timeout = close_timeout
        self._backoff = backoff
        self._listeners: list[Callable[[HeartRateEvent], None]] = []
        self._state = ConnectionState.DISCONNECTED
        self._task: asyncio.Task[None] | None = None
        self._client: Any = None  # identity marks the live connection
        self._battery: int | None = None

    def add_listener(self, cb: Callable[[HeartRateEvent], None]) -> None:
        self._listeners.append(cb)

    async def scan(self, timeout: float = 5.0) -> list[DeviceInfo]:
        """Heart rate sensors, Wahoo straps first; trainers are left out."""
        try:
            found = await self._scanner.discover(timeout=timeout, return_adv=True)
        except (Exception, asyncio.CancelledError) as e:
            reraise_own_cancel(e)
            raise TrainerError(f"Bluetooth scan failed: {describe_error(e)}") from e
        devices = []
        for dev, adv in found.values():
            uuids = {u.lower() for u in (*adv.service_uuids, *adv.service_data)}
            name = adv.local_name or dev.name or ""
            if FTMS_SERVICE not in uuids and (HR_SERVICE in uuids or is_wahoo(name)):
                devices.append(DeviceInfo(name or "Heart rate sensor", dev.address, adv.rssi))
        return sorted(devices, key=lambda d: (not is_wahoo(d.name), -d.rssi))

    async def connect(self, addr: str) -> None:
        await self.disconnect()
        self._set_state(ConnectionState.CONNECTING)
        dropped = asyncio.Event()
        task = asyncio.ensure_future(self._establish(addr, dropped))
        self._task = task
        try:
            await task
        except BaseException as e:
            if self._task is task:  # else disconnect() already took over
                self._task = None
                task.cancel()
                self._set_state(ConnectionState.DISCONNECTED)
            if isinstance(e, asyncio.CancelledError):
                reraise_own_cancel(e)
                raise TrainerError("Connecting was cancelled") from None
            raise
        if self._task is task:
            self._task = asyncio.ensure_future(self._keep(addr, dropped))

    async def disconnect(self) -> None:
        task, self._task = self._task, None
        client, self._client = self._client, None
        try:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.wait({task}, timeout=self._close_timeout + 1)
            if client is not None:
                await self._close(client)
        finally:
            self._set_state(ConnectionState.DISCONNECTED)

    async def _establish(self, addr: str, dropped: asyncio.Event) -> None:
        """Open a connection and make it current, or raise TrainerError with all cleaned up."""
        client: Any = None
        try:
            async with asyncio.timeout(self._connect_timeout):
                device = await self._scanner.find_device_by_address(
                    addr, timeout=self._find_timeout
                )
                if device is None:
                    raise TrainerError("Heart rate strap not found. Is it worn and moistened?")
                client = self._client_factory(
                    device, disconnected_callback=lambda _c: dropped.set()
                )
                await client.connect()
                if client.services.get_service(HR_SERVICE) is None:
                    raise TrainerError("This device does not provide heart rate")
                self._battery = await self._read_battery(client)
                await client.start_notify(
                    HR_MEASUREMENT, lambda _c, data: self._on_data(client, bytes(data))
                )
            if dropped.is_set():
                raise TrainerError("Connection to the heart rate strap was lost")
        except BaseException as e:
            if client is not None:
                await self._close(client)
            reraise_own_cancel(e)
            if isinstance(e, TrainerError):
                raise
            if isinstance(e, TimeoutError):
                raise TrainerError("Connecting to the heart rate strap timed out") from e
            if not isinstance(e, Exception | asyncio.CancelledError):
                raise
            raise TrainerError(f"Could not connect to the strap: {describe_error(e)}") from e
        self._client = client
        self._set_state(ConnectionState.CONNECTED)

    async def _keep(self, addr: str, dropped: asyncio.Event) -> None:
        """Reconnect after every drop until cancelled by disconnect()."""
        while True:
            await dropped.wait()
            client, self._client = self._client, None
            self._set_state(ConnectionState.RECONNECTING)
            if client is not None:
                await self._close(client)
            delay, limit = self._backoff
            while True:
                await asyncio.sleep(delay)
                dropped = asyncio.Event()
                try:
                    await self._establish(addr, dropped)
                    break
                except TrainerError as e:
                    log.info("strap reconnect failed: %s", e)
                delay = min(delay * 2, limit)

    async def _read_battery(self, client: Any) -> int | None:
        try:
            async with asyncio.timeout(3):
                data = await client.read_gatt_char(BATTERY_LEVEL)
            return data[0] if data and data[0] <= 100 else None
        except (Exception, asyncio.CancelledError) as e:
            reraise_own_cancel(e)
            log.info("no battery level: %s", describe_error(e))
            return None

    async def _close(self, client: Any) -> None:
        try:
            async with asyncio.timeout(self._close_timeout):
                await client.disconnect()
        except (Exception, asyncio.CancelledError) as e:
            reraise_own_cancel(e)
            log.info("strap disconnect failed: %s", describe_error(e))

    def _on_data(self, client: Any, data: bytes) -> None:
        if client is self._client:
            self._emit(HeartRate(self._clock(), parse_hr_measurement(data), self._battery))

    def _set_state(self, state: ConnectionState) -> None:
        if state is not self._state:
            self._state = state
            self._emit(state)

    def _emit(self, event: HeartRateEvent) -> None:
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:
                log.exception("strap listener failed")
