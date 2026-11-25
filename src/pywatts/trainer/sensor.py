"""What heart rate straps and power meters have in common: scan, link, reconnect, battery."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from bleak import BleakClient, BleakScanner

from pywatts.trainer.base import ConnectionState, DeviceInfo, TrainerError
from pywatts.trainer.ftms import describe_error, find_device, ftms_uuid, reraise_own_cancel
from pywatts.trainer.radio import Radio

log = logging.getLogger(__name__)

BATTERY_LEVEL = ftms_uuid(0x2A19)


class _Link:
    """One connection attempt and what came of it."""

    def __init__(self) -> None:
        self.client: Any = None
        self.dropped = asyncio.Event()
        self.battery: int | None = None


class Sensor:
    """Base class. `client_factory`/`scanner` mirror bleak."""

    NOUN = "sensor"
    UNNAMED = "Sensor"
    SERVICE = ""
    MEASUREMENT = ""
    NO_SERVICE = "This device is not a suitable sensor"
    HINT = "Is it awake and in range?"

    def __init__(
        self,
        client_factory: Callable[..., Any] = BleakClient,
        scanner: Any = BleakScanner,
        *,
        radio: Radio | None = None,
        clock: Callable[[], float] = time.monotonic,
        find_timeout: float = 10.0,
        connect_timeout: float = 30.0,
        close_timeout: float = 2.0,
        backoff: tuple[float, float] = (1.0, 30.0),
    ) -> None:
        self._client_factory = client_factory
        self._scanner = scanner
        self._radio = radio or Radio()
        self._clock = clock
        self._find_timeout = find_timeout
        self._connect_timeout = connect_timeout
        self._close_timeout = close_timeout
        self._backoff = backoff
        self._listeners: list[Callable[[Any], None]] = []
        self._state = ConnectionState.DISCONNECTED
        self._task: asyncio.Task[Any] | None = None
        self._client: Any = None  # identity marks the live connection
        self._battery: int | None = None

    # --- device specifics ---
    def _accepts(self, name: str, uuids: set[str]) -> bool:
        raise NotImplementedError

    def _order(self, device: DeviceInfo) -> Any:
        return -device.rssi

    def _reset(self) -> None:
        """A connection begins."""

    def _reading(self, data: bytes) -> Any:
        """The event for a notification; None = nothing to report."""
        raise NotImplementedError

    # --- public API ---
    def add_listener(self, cb: Callable[[Any], None]) -> None:
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
            name = adv.local_name or dev.name or ""
            if self._accepts(name, uuids):
                devices.append(DeviceInfo(name or self.UNNAMED, dev.address, adv.rssi))
        return sorted(devices, key=self._order)

    async def connect(self, addr: str) -> None:
        await self.disconnect()
        self._set_state(ConnectionState.CONNECTING)
        task = asyncio.ensure_future(self._establish(addr))
        self._task = task
        try:
            dropped = await task
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

    # --- connection ---
    async def _establish(self, addr: str) -> asyncio.Event:
        """Open a connection and make it current, or raise TrainerError with all cleaned up.

        Returns the event that tells of its loss."""
        link = _Link()

        async def attempt() -> None:  # runs again if the trainer needed the radio
            nonlocal link
            link = _Link()
            await self._attempt(link, addr)

        try:
            await self._radio.run(attempt)
            if link.dropped.is_set():
                raise TrainerError(f"Connection to the {self.NOUN} was lost")
        except BaseException as e:
            await self._hang_up(link)
            reraise_own_cancel(e)
            if isinstance(e, TrainerError):
                raise
            if isinstance(e, TimeoutError):
                raise TrainerError(f"Connecting to the {self.NOUN} timed out") from e
            if not isinstance(e, Exception | asyncio.CancelledError):
                raise
            raise TrainerError(f"Could not connect to the {self.NOUN}: {describe_error(e)}") from e
        # No await from here on: a drop is either seen above or handled by _keep.
        self._client = link.client
        self._battery = link.battery
        self._set_state(ConnectionState.CONNECTED)
        return link.dropped

    async def _attempt(self, link: _Link, addr: str) -> None:
        try:
            async with asyncio.timeout(self._connect_timeout):
                device = await find_device(self._scanner, addr, self._find_timeout)
                if device is None:
                    raise TrainerError(f"The {self.NOUN} was not found. {self.HINT}")
                client = link.client = self._client_factory(
                    device, disconnected_callback=lambda _c: link.dropped.set()
                )
                await client.connect()
                if client.services.get_service(self.SERVICE) is None:
                    raise TrainerError(self.NO_SERVICE)
                link.battery = await self._read_battery(client)
                self._reset()
                await client.start_notify(
                    self.MEASUREMENT, lambda _c, data: self._on_data(client, bytes(data))
                )
        except BaseException:
            await self._hang_up(link)  # before someone else gets the radio
            raise

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
                try:
                    dropped = await self._establish(addr)
                    break
                except TrainerError as e:
                    log.info("%s reconnect failed: %s", self.NOUN, e)
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

    async def _hang_up(self, link: _Link) -> None:
        client, link.client = link.client, None
        if client is not None:
            await self._close(client)

    async def _close(self, client: Any) -> None:
        try:
            async with asyncio.timeout(self._close_timeout):
                await client.disconnect()
        except (Exception, asyncio.CancelledError) as e:
            reraise_own_cancel(e)
            log.info("%s disconnect failed: %s", self.NOUN, describe_error(e))

    # --- callbacks (on the loop thread) ---
    def _on_data(self, client: Any, data: bytes) -> None:
        if client is self._client:
            event = self._reading(data)
            if event is not None:
                self._emit(event)

    def _set_state(self, state: ConnectionState) -> None:
        if state is not self._state:
            self._state = state
            self._emit(state)

    def _emit(self, event: Any) -> None:
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:
                log.exception("%s listener failed", self.NOUN)
