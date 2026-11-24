import asyncio
from types import SimpleNamespace

import pytest
from bleak.exc import BleakError

from pywatts.trainer.base import ConnectionState as S
from pywatts.trainer.base import HeartRate, TrainerError
from pywatts.trainer.fake import FakeHrStrap
from pywatts.trainer.fake import FakeTrainer as Trainer
from pywatts.trainer.ftms import FTMS_SERVICE, HR_MEASUREMENT, HR_SERVICE
from pywatts.trainer.hr import HrStrap
from pywatts.trainer.thread import TrainerBridge


class Client:
    def __init__(self, rig, on_drop):
        self.rig, self.on_drop = rig, on_drop
        self.services = self
        self.disconnects = 0

    def get_service(self, uuid):
        return object() if uuid == HR_SERVICE and self.rig.has_hr else None

    async def connect(self):
        if self.rig.hang:
            await asyncio.sleep(60)
        if self.rig.fail:
            self.rig.fail -= 1
            raise BleakError("nope")

    async def disconnect(self):
        self.disconnects += 1
        self.on_drop(self)

    async def read_gatt_char(self, _uuid):
        return bytearray(self.rig.battery)

    async def start_notify(self, uuid, cb):
        assert uuid == HR_MEASUREMENT
        self.cb = cb


class Rig:
    def __init__(self):
        self.has_hr, self.hang, self.fail, self.battery = True, False, 0, b"\x50"
        self.clients, self.events = [], []
        self.strap = HrStrap(self.make, self, clock=lambda: 1.0, backoff=(0.01, 0.02))
        self.strap.add_listener(self.events.append)

    def make(self, _device, disconnected_callback):
        self.clients.append(Client(self, disconnected_callback))
        return self.clients[-1]

    async def find_device_by_address(self, addr, timeout):
        return None if addr == "missing" else SimpleNamespace(address=addr)

    async def discover(self, timeout, return_adv):
        rows = [
            ("a", "Other HRM", -30, [HR_SERVICE]),
            ("b", "TICKR 12AB", -70, [HR_SERVICE.upper()]),
            ("c", "TRACKR HR", -60, []),
            ("d", "KICKR CORE", -20, [FTMS_SERVICE, HR_SERVICE]),
            ("e", "Headset", -10, []),
        ]
        return {
            addr: (
                SimpleNamespace(address=addr, name=None),
                SimpleNamespace(local_name=n, service_uuids=u, service_data={}, rssi=r),
            )
            for addr, n, r, u in rows
        }

    @property
    def states(self):
        return [e for e in self.events if isinstance(e, S)]


def run(coro):
    async def wrapped():
        async with asyncio.timeout(10):
            await coro

    asyncio.run(wrapped())


async def until(cond):
    while not cond():
        await asyncio.sleep(0.002)


def test_scan_prefers_wahoo_and_skips_trainers():
    async def go():
        assert [d.addr for d in await Rig().strap.scan(0.1)] == ["c", "b", "a"]

    run(go())


def test_readings_contact_and_stale_client():
    async def go():
        rig = Rig()
        await rig.strap.connect("b")
        client = rig.clients[0]
        for data in (b"\x00\x78", b"\x01\x2c\x01", b"\x04\x78", b""):
            client.cb(None, bytearray(data))
        assert rig.events[2:] == [HeartRate(1.0, v, 80) for v in (120, 300, None, None)]
        await rig.strap.disconnect()
        n = len(rig.events)
        client.cb(None, bytearray(b"\x00\x78"))
        assert len(rig.events) == n and rig.states == [S.CONNECTING, S.CONNECTED, S.DISCONNECTED]
        assert len(asyncio.all_tasks()) == 1 and client.disconnects == 1

    run(go())


def test_reconnects_until_disconnect():
    async def go():
        rig = Rig()
        await rig.strap.connect("b")
        rig.fail = 2
        rig.clients[0].on_drop(None)
        await until(lambda: len(rig.clients) == 4 and rig.states[-1] is S.CONNECTED)
        rig.clients[-1].on_drop(None)  # a second drop is noticed too
        await until(lambda: len(rig.clients) == 5 and rig.states[-1] is S.CONNECTED)
        rig.hang = True
        rig.clients[-1].on_drop(None)
        await until(lambda: len(rig.clients) == 6)
        await rig.strap.disconnect()
        assert rig.states[-1] is S.DISCONNECTED and len(asyncio.all_tasks()) == 1

    run(go())


def test_failures_clean_up():
    async def go():
        for setup in ("missing", "fail", "no_hr"):
            rig = Rig()
            rig.fail, rig.has_hr = int(setup == "fail"), setup != "no_hr"
            with pytest.raises(TrainerError):
                await rig.strap.connect("missing" if setup == "missing" else "b")
            assert rig.states == [S.CONNECTING, S.DISCONNECTED]
            assert all(c.disconnects == 1 for c in rig.clients)
            assert len(asyncio.all_tasks()) == 1
        rig = Rig()
        rig.battery = b""
        await rig.strap.connect("b")  # no battery level is fine
        await rig.strap.disconnect()

    run(go())


def test_cancel_hanging_connect():
    async def go():
        for how in ("cancel", "disconnect"):
            rig = Rig()
            rig.hang = True
            task = asyncio.ensure_future(rig.strap.connect("b"))
            await until(lambda rig=rig: rig.clients)
            if how == "cancel":
                task.cancel()
            else:
                await rig.strap.disconnect()
            with pytest.raises((asyncio.CancelledError, TrainerError)):
                await task
            await asyncio.sleep(0.01)
            assert rig.states[-1] is S.DISCONNECTED and len(asyncio.all_tasks()) == 1
            assert rig.clients[0].disconnects == 1

    run(go())


def test_bridge_strap_is_independent_of_trainer(qtbot):
    bridge = TrainerBridge(Trainer, hr_factory=FakeHrStrap)
    try:
        with qtbot.waitSignal(bridge.hr_scan_finished) as scan:
            bridge.hr_scan(0.1)
        with qtbot.waitSignal(bridge.hr_reading, timeout=5000) as reading:
            bridge.hr_connect(scan.args[0][0].addr)
        assert reading.args[0].bpm > 0 and bridge.hr_state is S.CONNECTED
        assert bridge.state is S.DISCONNECTED
        bridge.hr_disconnect()
        qtbot.waitUntil(lambda: bridge.hr_state is S.DISCONNECTED)
        bridge.hr_connect("x")
        qtbot.waitUntil(lambda: bridge.hr_state is S.CONNECTED)
    finally:
        bridge.shutdown()
    assert not bridge._worker._thread.is_alive()


def test_bridge_cancels_hanging_strap_connect(qtbot):
    made = []

    def factory():
        made.append(Rig())
        made[0].hang = True
        return made[0].strap

    bridge = TrainerBridge(Trainer, hr_factory=factory)
    errors = []
    bridge.hr_error.connect(errors.append)
    try:
        bridge.hr_connect("b")
        qtbot.waitUntil(lambda: bool(made and made[0].clients))
        bridge.hr_disconnect()
        qtbot.waitUntil(lambda: made[0].states[-1] is S.DISCONNECTED)
        bridge.hr_connect("b")
        qtbot.waitUntil(lambda: len(made[0].clients) == 2)
    finally:
        bridge.shutdown()
    assert not bridge._worker._thread.is_alive() and errors == []


def test_bridge_without_strap_support(qtbot):
    bridge = TrainerBridge(Trainer)
    try:
        with qtbot.waitSignal(bridge.hr_error):
            bridge.hr_connect("b")
    finally:
        bridge.shutdown()
