import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest
from bleak.exc import BleakBluetoothNotAvailableError, BleakDBusError

from pywatts import platform
from pywatts.trainer import ftms
from pywatts.trainer.base import ConnectionState as S
from pywatts.trainer.base import TrainerError
from pywatts.trainer.hr import HrStrap
from pywatts.trainer.radio import Radio


def run(coro):
    async def wrapped():
        async with asyncio.timeout(10):
            await coro
            assert len(asyncio.all_tasks()) == 1  # nothing left over

    asyncio.run(wrapped())


async def until(cond):
    while not cond():
        await asyncio.sleep(0.002)


def test_one_at_a_time_and_results():
    async def go():
        radio, active, most = Radio(), 0, 0

        async def op(value):
            nonlocal active, most
            active += 1
            most = max(most, active)
            await asyncio.sleep(0.005)
            active -= 1
            if value == 2:
                raise ValueError
            return value

        results = await asyncio.gather(
            *(radio.run(lambda v=v: op(v), priority=v == 3) for v in range(4)),
            return_exceptions=True,
        )
        assert most == 1
        assert results[0] == 0 and results[1] == 1 and results[3] == 3
        assert isinstance(results[2], ValueError)

    run(go())


def test_priority_interrupts_and_the_other_is_repeated():
    async def go():
        radio, log = Radio(), []
        free = asyncio.Event()

        async def sensor():
            log.append("sensor")
            try:
                await free.wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.01)  # hanging up
                log.append("hung up")
                raise
            return "found"

        async def trainer():
            log.append("trainer")
            await asyncio.sleep(0.01)
            free.set()

        task = asyncio.ensure_future(radio.run(sensor))
        await until(lambda: log)
        other = asyncio.ensure_future(radio.run(sensor))  # waits; must not overtake the trainer
        await asyncio.sleep(0)
        await radio.run(trainer, priority=True)
        assert log[:3] == ["sensor", "hung up", "trainer"]
        assert await task == "found" and await other == "found"

    run(go())


def test_hanging_operation_does_not_block_the_trainer():
    async def go():
        radio = Radio(yield_timeout=0.02)
        release = asyncio.Event()

        async def stubborn():
            while not release.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await release.wait()
            return "late"

        async def op():
            return "ok"

        task = asyncio.ensure_future(radio.run(stubborn))
        await asyncio.sleep(0.005)
        assert await radio.run(op, priority=True) == "ok"
        assert await radio.run(op) == "ok"  # it lost its turn
        release.set()
        assert await task == "late"
        assert await radio.run(op) == "ok"

    run(go())


def test_cancelled_caller_and_aborted_operation():
    async def go():
        radio = Radio()
        cleaned = []

        async def wait():
            try:
                await asyncio.sleep(60)
            finally:
                cleaned.append(1)

        async def aborted():
            raise asyncio.CancelledError  # as bleak does when Windows aborts something

        async def op():
            return 1

        holder = asyncio.ensure_future(radio.run(wait))
        waiting = asyncio.ensure_future(radio.run(wait, priority=False))
        await asyncio.sleep(0.005)
        for task in (waiting, holder):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert cleaned == [1]
        urgent = asyncio.ensure_future(radio.run(wait, priority=True))
        await asyncio.sleep(0.005)
        urgent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await urgent
        with pytest.raises(asyncio.CancelledError):
            await radio.run(aborted)
        assert await radio.run(op) == 1

    run(go())


class BlueZ:
    """Scanner and client factory of one adapter; overlapping operations fail as in BlueZ."""

    def __init__(self):
        self.busy = False
        self.missing = set()
        self.find_s = 0.01
        self.finds = []
        self.clients = []
        self.connected = set()

    @contextlib.asynccontextmanager
    async def op(self):
        if self.busy:
            raise BleakDBusError("org.bluez.Error.InProgress", [])
        self.busy = True
        try:
            yield
        finally:
            self.busy = False

    async def discover(self, timeout, return_adv):
        async with self.op():
            await asyncio.sleep(0.01)
        return {}

    async def find_device_by_address(self, addr, timeout):
        self.finds.append(addr)
        async with self.op():
            await asyncio.sleep(self.find_s)
        return None if addr in self.missing else SimpleNamespace(address=addr)

    def __call__(self, device, disconnected_callback):
        client = Client(self, device.address, disconnected_callback)
        self.clients.append(client)
        return client


class Client:
    def __init__(self, bluez, addr, on_drop):
        self.bluez, self.addr, self.on_drop = bluez, addr, on_drop
        self.services = self
        self.callbacks = {}
        self.writes = []

    def get_service(self, uuid):
        return object() if uuid in (ftms.FTMS_SERVICE, ftms.HR_SERVICE) else None

    async def connect(self):
        async with self.bluez.op():
            await asyncio.sleep(0.01)
        self.bluez.connected.add(self.addr)

    async def disconnect(self):
        self.bluez.connected.discard(self.addr)
        self.on_drop(self)

    async def start_notify(self, char, cb):
        self.callbacks[char] = cb

    async def read_gatt_char(self, _char):
        return bytearray(b"")

    async def write_gatt_char(self, _char, data, response):
        self.writes.append(bytes(data))
        self.callbacks[ftms.CONTROL_POINT](None, bytearray([0x80, data[0], 1]))


def pair(bluez, **strap_args):
    radio = Radio()
    trainer = ftms.FtmsTrainer(bluez, bluez, radio=radio, backoff=(0.01, 0.02))
    strap = HrStrap(bluez, bluez, radio=radio, backoff=(0.01, 0.02), **strap_args)
    return trainer, strap


def test_trainer_and_strap_connect_together():
    async def go():
        bluez = BlueZ()
        trainer, strap = pair(bluez)
        await asyncio.gather(strap.connect("s"), trainer.connect("t"), strap.scan(), trainer.scan())
        assert bluez.connected == {"s", "t"}
        for client in list(bluez.clients):  # both links drop at the same moment
            client.on_drop(client)
        n = len(bluez.clients)
        await until(lambda: len(bluez.clients) >= n + 2 and len(bluez.connected) == 2)
        await until(lambda: trainer._state is S.CONNECTED and strap._state is S.CONNECTED)
        await asyncio.gather(trainer.disconnect(), strap.disconnect())
        assert not bluez.connected

    run(go())


def test_strap_search_does_not_delay_the_trainer_or_its_control():
    async def go():
        bluez = BlueZ()
        trainer, strap = pair(bluez)
        await trainer.connect("t")
        bluez.find_s = 60
        task = asyncio.ensure_future(strap.connect("s"))
        await until(lambda: "s" in bluez.finds)
        t0 = time.monotonic()
        await trainer.set_target_power(200)
        await trainer.pause()
        bluez.clients[0].on_drop(bluez.clients[0])
        await until(lambda: bluez.finds[-1] == "t" and len(bluez.finds) == 3)
        assert time.monotonic() - t0 < 2
        await trainer.disconnect()
        await strap.disconnect()
        with pytest.raises((TrainerError, asyncio.CancelledError)):
            await task

    run(go())


def test_left_over_link_is_released_once_and_only_if_not_found(monkeypatch):
    async def go():
        bluez = BlueZ()
        released = []

        async def release(addr):
            released.append(addr)
            bluez.missing.discard(addr)
            return addr != "gone"

        monkeypatch.setattr(platform, "release_bluetooth_link", release)
        trainer, strap = pair(bluez)
        await trainer.connect("t")
        assert released == []
        bluez.missing = {"s"}
        await strap.connect("s")
        assert released == ["s"] and bluez.finds == ["t", "s", "s"]
        await strap.disconnect()
        bluez.missing = {"gone"}
        with pytest.raises(TrainerError):
            await strap.connect("gone")
        assert bluez.finds[-1] == "gone" and bluez.finds.count("gone") == 1
        await trainer.disconnect()

    run(go())


def test_left_over_link_calls(monkeypatch):
    async def go():
        calls = []

        async def tool(*args):
            calls.append(args)
            return "Device\n\tConnected: yes\n" if args[0] == "info" and "01" in args[1] else ""

        monkeypatch.setattr(platform, "_bluetoothctl", tool)
        addr, other = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"
        monkeypatch.setattr(platform.sys, "platform", "win32")
        assert not await platform.release_bluetooth_link(addr) and calls == []
        monkeypatch.setattr(platform.sys, "platform", "linux")
        for bad in ("", "x", addr + ";ls", "--help"):
            assert not await platform.release_bluetooth_link(bad)
        assert calls == []
        assert not await platform.release_bluetooth_link(other)
        assert await platform.release_bluetooth_link(addr)
        assert calls == [("info", other), ("info", addr), ("disconnect", addr)]

    run(go())


def test_missing_tool_is_no_link(monkeypatch):
    async def go():
        monkeypatch.setenv("PATH", "")
        assert await platform._bluetoothctl("info", "AA:BB:CC:DD:EE:FF") == ""

    run(go())


def test_error_texts():
    def text(name):
        return ftms.describe_error(BleakDBusError(name, []))

    assert "bluez" in text("org.freedesktop.DBus.Error.ServiceUnknown")
    assert "off" in text("org.bluez.Error.NotReady")
    assert "InProgress" in text("org.bluez.Error.InProgress")
    off = ftms.describe_error(BleakBluetoothNotAvailableError("No adapters.", None))
    assert off == "No adapters. Is Bluetooth switched on?"
    assert ftms.describe_error(ValueError()) == "ValueError"
