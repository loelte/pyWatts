import asyncio
import threading
import time

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QObject

from pywatts.trainer.base import ConnectionState as S
from pywatts.trainer.base import TrainerError
from pywatts.trainer.fake import FakeTrainer
from pywatts.trainer.thread import TrainerBridge


class Recorder:
    def __init__(self):
        self.calls = []
        self.listeners = []
        self.hang = set()
        self.gates = {}
        self.cancelled = []
        self.thread = threading.current_thread()

    def add_listener(self, cb):
        self.listeners.append(cb)

    def emit(self, event):
        for cb in self.listeners:
            cb(event)

    async def _call(self, name, *args):
        self.calls.append((name, *args))
        try:
            if name in self.hang:
                await asyncio.sleep(60)
            while name in self.gates and not self.gates[name].is_set():
                await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            self.cancelled.append(name)
            raise

    async def scan(self, timeout=5.0):
        await self._call("scan", timeout)
        return []

    async def connect(self, addr):
        if addr == "bad":
            raise TrainerError("no such trainer")
        self.emit(S.CONNECTING)
        try:
            await self._call("connect", addr)
        except asyncio.CancelledError:
            self.emit(S.DISCONNECTED)
            raise
        self.emit(S.CONNECTED)

    async def disconnect(self):
        await self._call("disconnect")
        self.emit(S.DISCONNECTED)

    async def set_target_power(self, w):
        await self._call("set", w)

    async def set_open_mode(self):
        await self._call("open")

    async def start(self):
        await self._call("start")

    async def pause(self):
        await self._call("pause")

    async def stop(self):
        await self._call("stop")


@pytest.fixture
def made(qtbot):
    trainers = []

    def factory():
        trainers.append(Recorder())
        return trainers[-1]

    bridge = TrainerBridge(factory)
    qtbot.waitUntil(lambda: bool(trainers))
    yield bridge, trainers[0]
    if not sip.isdeleted(bridge):
        bridge.shutdown(1.0)


def names(trainer):
    return [c[0] for c in trainer.calls]


def test_factory_on_ble_thread_and_order(qtbot, made):
    bridge, t = made
    assert t.thread is not threading.current_thread()
    seen = []
    bridge.connection_changed.connect(
        lambda s: seen.append((s, bridge.state, threading.current_thread()))
    )
    bridge.connect_trainer("a")
    bridge.start()
    bridge.pause()
    bridge.stop()
    # disconnect cancels a pending connect by design, so let it finish first
    qtbot.waitUntil(lambda: len(seen) == 2)
    bridge.disconnect_trainer()
    qtbot.waitUntil(lambda: len(seen) == 3)
    assert names(t) == ["connect", "start", "pause", "stop", "disconnect"]
    assert [s[0] for s in seen] == [S.CONNECTING, S.CONNECTED, S.DISCONNECTED]
    assert all(s == state and th is threading.main_thread() for s, state, th in seen)


def test_target_coalescing(qtbot, made):
    bridge, t = made
    gate = t.gates["set"] = threading.Event()
    bridge.set_target_power(1)
    qtbot.waitUntil(lambda: t.calls == [("set", 1)])
    for w in range(2, 50):
        bridge.set_target_power(w)
    bridge.start()
    bridge.set_target_power(99)
    time.sleep(0.05)
    gate.set()
    qtbot.waitUntil(lambda: len(t.calls) == 3)
    assert t.calls == [("set", 1), ("start",), ("set", 99)]


def test_error_signal(qtbot, made):
    bridge, _ = made
    with qtbot.waitSignal(bridge.error) as sig:
        bridge.connect_trainer("bad")
    assert sig.args == ["no such trainer"]


def test_factory_failure(qtbot):
    def factory():
        raise RuntimeError("no adapter")

    bridge = TrainerBridge(factory)
    with qtbot.waitSignal(bridge.error) as sig:
        pass
    assert "no adapter" in sig.args[0]
    with qtbot.waitSignals([bridge.error, bridge.scan_finished]):
        bridge.scan()
    bridge.shutdown()


def test_disconnect_cancels_hanging_connect(qtbot, made):
    bridge, t = made
    errors = []
    bridge.error.connect(errors.append)
    t.hang.add("connect")
    bridge.connect_trainer("a")
    qtbot.waitUntil(lambda: bridge.state is S.CONNECTING)
    bridge.connect_trainer("b")  # queued, must never run
    bridge.disconnect_trainer()
    qtbot.waitUntil(lambda: bridge.state is S.DISCONNECTED and "disconnect" in names(t))
    assert t.cancelled == ["connect"] and t.calls[-1] == ("disconnect",)
    assert errors == []


def test_scan_does_not_block_control(qtbot, made):
    bridge, t = made
    t.gates["scan"] = threading.Event()
    bridge.scan(1.0)
    bridge.set_target_power(5)
    bridge.connect_trainer("a")
    qtbot.waitUntil(lambda: ("set", 5) in t.calls)
    assert names(t) == ["scan", "set"]
    with qtbot.waitSignal(bridge.scan_finished):
        t.gates["scan"].set()
    qtbot.waitUntil(lambda: bridge.state is S.CONNECTED)


@pytest.mark.parametrize("hang", [{"connect"}, {"connect", "stop", "disconnect"}])
def test_shutdown_bounded_idempotent_and_silent(qtbot, made, hang):
    bridge, t = made
    t.hang |= hang
    bridge.connect_trainer("a")
    qtbot.waitUntil(lambda: names(t) == ["connect"])
    seen = []
    for sig in (bridge.connection_changed, bridge.error, bridge.telemetry):
        sig.connect(seen.append)
    t0 = time.monotonic()
    bridge.shutdown(1.0)
    bridge.shutdown(1.0)
    assert time.monotonic() - t0 < 1.5
    assert names(t) == ["connect", "stop", "disconnect"]
    assert "connect" in t.cancelled
    assert not bridge._worker._thread.is_alive()
    seen.clear()
    bridge.start()
    t.emit(S.CONNECTED)
    qtbot.wait(20)
    assert seen == [] and bridge.state is S.DISCONNECTED


def test_shutdown_right_after_construction(qtbot):
    bridge = TrainerBridge(FakeTrainer)
    bridge.shutdown()
    assert not bridge._worker._thread.is_alive()


def test_deleted_without_shutdown(qtbot):
    parent = QObject()
    trainers = []

    def factory():
        trainers.append(FakeTrainer(time_scale=1000))
        return trainers[0]

    bridge = TrainerBridge(factory, parent)
    worker = bridge._worker
    bridge.connect_trainer("x")
    qtbot.waitUntil(lambda: bridge.state is S.CONNECTED)
    sip.delete(parent)
    worker.post("error", "late")
    assert worker.join(5.0)


def test_with_fake_trainer(qtbot):
    bridge = TrainerBridge(lambda: FakeTrainer(time_scale=200, seed=1))
    with qtbot.waitSignal(bridge.scan_finished) as sig:
        bridge.scan()
    bridge.connect_trainer(sig.args[0][0].addr)
    bridge.start()
    bridge.set_target_power(200)
    got = []
    bridge.telemetry.connect(got.append)
    qtbot.waitUntil(lambda: len(got) > 20 and got[-1].power_w > 150)
    bridge.shutdown()
