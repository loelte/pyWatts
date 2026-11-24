import asyncio
import inspect
from types import SimpleNamespace

import pytest
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from pywatts.trainer import ftms
from pywatts.trainer.base import ConnectionState as S
from pywatts.trainer.base import Telemetry, TrainerError


class Rig:
    """Fake bleak: is both the scanner and the client factory."""

    def __init__(self, **trainer_args):
        self.clients = []
        self.results = {}  # op -> list of results to hand out, then success
        self.silent = set()
        self.features = {ftms.POWER_RANGE: b""}
        self.has_hr = False
        self.has_ftms = True
        self.hang_connect = False
        self.fail_connects = 0
        self.max_parallel = 0
        self.gate = None
        self.parallel = 0
        args = dict(command_timeout=0.1, resend_interval=0.03, backoff=(0.01, 0.02))
        self.trainer = ftms.FtmsTrainer(self.make, self, **{**args, **trainer_args})
        self.events = []
        self.trainer.add_listener(self.events.append)

    async def discover(self, timeout, return_adv):
        def entry(addr, name, rssi, uuids):
            adv = SimpleNamespace(local_name=name, service_uuids=uuids, service_data={}, rssi=rssi)
            return addr, (SimpleNamespace(address=addr, name=None), adv)

        return dict(
            [
                entry("a", "Weak", -80, [ftms.FTMS_SERVICE]),
                entry("b", "Headset", -20, []),
                entry("c", None, -40, [ftms.FTMS_SERVICE.upper()]),
            ]
        )

    async def find_device_by_address(self, addr, timeout):
        return None if addr == "missing" else SimpleNamespace(address=addr)

    def make(self, device, disconnected_callback):
        client = FakeClient(self, disconnected_callback)
        self.clients.append(client)
        return client

    @property
    def client(self):
        return self.clients[-1]

    @property
    def states(self):
        return [e for e in self.events if isinstance(e, S)]

    @property
    def telemetry(self):
        return [e for e in self.events if isinstance(e, Telemetry)]


class FakeClient:
    def __init__(self, rig, on_disconnect):
        self.rig = rig
        self.on_disconnect = on_disconnect
        self.callbacks = {}
        self.writes = []
        self.connected = False
        self.disconnects = 0
        self.services = self

    def get_service(self, uuid):
        ok = {ftms.FTMS_SERVICE: self.rig.has_ftms, ftms.HR_SERVICE: self.rig.has_hr}
        return object() if ok.get(uuid) else None

    async def connect(self):
        if self.rig.hang_connect:
            await asyncio.sleep(60)
        if self.rig.fail_connects:
            self.rig.fail_connects -= 1
            raise BleakError("nope")
        self.connected = True

    async def disconnect(self):
        self.disconnects += 1
        if self.connected:
            self.connected = False
            self.on_disconnect(self)  # bleak WinRT synthesizes the callback too

    async def start_notify(self, char, cb):
        self.callbacks[char] = cb

    async def read_gatt_char(self, char):
        if not self.rig.features.get(char):
            raise BleakError("not found")
        return bytearray(self.rig.features[char])

    async def write_gatt_char(self, char, data, response):
        assert char == ftms.CONTROL_POINT and response
        rig = self.rig
        rig.parallel += 1
        rig.max_parallel = max(rig.max_parallel, rig.parallel)
        self.writes.append(bytes(data))
        op = data[0]
        if rig.gate is not None and rig.gate[0] == op:
            await rig.gate[1].wait()
        await asyncio.sleep(0)
        rig.parallel -= 1
        if op in rig.silent:
            return
        queue = rig.results.get(op)
        result = queue.pop(0) if queue else 1
        self.notify(ftms.CONTROL_POINT, bytes([0x80, op, result]))

    def notify(self, char, data):
        self.callbacks[char](None, bytearray(data))

    def drop(self):
        self.connected = False
        self.on_disconnect(self)

    @property
    def ops(self):
        return [w[0] for w in self.writes]


def run(coro):
    async def wrapped():
        async with asyncio.timeout(10):
            await coro

    asyncio.run(wrapped())


async def until(cond):
    while not cond():
        await asyncio.sleep(0.002)


async def connected(**kw):
    rig = Rig(**kw)
    await rig.trainer.connect("a")
    return rig


def test_fake_matches_installed_bleak():
    init = inspect.signature(BleakClient.__init__).parameters
    assert "disconnected_callback" in init
    assert "return_adv" in inspect.signature(BleakScanner.discover).parameters
    assert "response" in inspect.signature(BleakClient.write_gatt_char).parameters
    assert "timeout" in inspect.signature(BleakScanner.find_device_by_address).parameters
    assert hasattr(BleakClient, "start_notify") and hasattr(BleakClient, "read_gatt_char")


def test_scan_filters_and_sorts():
    async def go():
        found = await Rig().trainer.scan(0.1)
        assert [d.addr for d in found] == ["c", "a"]

    run(go())


def test_connect_sequence_and_disconnect():
    async def go():
        rig = await connected()
        assert rig.client.ops == [0x00, 0x07]
        assert rig.states == [S.CONNECTING, S.CONNECTED]
        await rig.trainer.set_target_power(150)
        assert rig.client.writes[-1] == b"\x05\x96\x00"
        await rig.trainer.disconnect()
        assert rig.client.writes[-1] == b"\x08\x01"  # load released before hanging up
        assert rig.states[-1] is S.DISCONNECTED and S.RECONNECTING not in rig.states
        assert not rig.client.connected
        with pytest.raises(TrainerError):
            await rig.trainer.set_target_power(1)

    run(go())


def test_failed_connects_clean_up():
    async def go():
        for setup in ("missing", "bleak", "noftms", "rejected", "silent"):
            rig = Rig()
            if setup == "bleak":
                rig.fail_connects = 1
            rig.has_ftms = setup != "noftms"
            if setup == "rejected":
                rig.results[0] = [4]
            if setup == "silent":
                rig.silent.add(0)
            with pytest.raises(TrainerError) as e:
                await rig.trainer.connect("missing" if setup == "missing" else "a")
            assert str(e.value)
            assert rig.states == [S.CONNECTING, S.DISCONNECTED]
            assert all(c.disconnects == 1 and not c.connected for c in rig.clients)
            assert len(asyncio.all_tasks()) == 1

    run(go())


def test_disconnect_cancels_hanging_connect():
    async def go():
        rig = Rig()
        rig.hang_connect = True
        task = asyncio.ensure_future(rig.trainer.connect("a"))
        await until(lambda: rig.clients)
        await rig.trainer.disconnect()
        with pytest.raises(TrainerError):
            await task
        assert rig.states == [S.CONNECTING, S.DISCONNECTED]
        assert rig.client.disconnects == 1

    run(go())


def test_cancelled_connect_cleans_up():
    async def go():
        rig = Rig()
        rig.hang_connect = True
        task = asyncio.ensure_future(rig.trainer.connect("a"))
        await until(lambda: rig.clients)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rig.states == [S.CONNECTING, S.DISCONNECTED]
        assert rig.client.disconnects == 1

    run(go())


def test_clamping():
    async def go():
        rig = await connected()
        await rig.trainer.set_target_power(9999)
        assert rig.client.writes[-1] == b"\x05\xd0\x07"
        await rig.trainer.set_target_power(-5)
        assert rig.client.writes[-1] == b"\x05\x00\x00"
        await rig.trainer.disconnect()
        rig.features[ftms.POWER_RANGE] = bytes.fromhex("1900e8030100")
        await rig.trainer.connect("a")
        await rig.trainer.set_target_power(1500)
        assert rig.client.writes[-1] == b"\x05\xe8\x03"
        await rig.trainer.set_target_power(0)
        assert rig.client.writes[-1] == b"\x05\x19\x00"
        await rig.trainer.disconnect()

    run(go())


def test_resend_only_while_started():
    async def go():
        rig = await connected()
        await rig.trainer.set_target_power(150)
        await until(lambda: rig.client.ops.count(5) >= 3)
        for halt, param in ((rig.trainer.pause, 2), (rig.trainer.stop, 1)):
            await halt()
            assert rig.client.writes[-1] == bytes([8, param])
            n = len(rig.client.writes)
            await asyncio.sleep(0.1)
            assert len(rig.client.writes) == n
        await rig.trainer.set_target_power(170)
        assert len(rig.client.writes) == n  # deferred until start
        await rig.trainer.start()
        assert rig.client.writes[n : n + 2] == [b"\x07", b"\x05\xaa\x00"]
        await rig.trainer.disconnect()

    run(go())


def test_not_supported_tolerated_for_start_pause_stop_only():
    async def go():
        rig = Rig()
        rig.results = {7: [2, 2], 8: [2, 2]}
        await rig.trainer.connect("a")
        await rig.trainer.start()
        await rig.trainer.pause()
        await rig.trainer.stop()
        await rig.trainer.start()
        rig.results[5] = [2]
        with pytest.raises(TrainerError, match="not supported"):
            await rig.trainer.set_target_power(100)
        rig.results[7] = [4]
        with pytest.raises(TrainerError, match="failed"):
            await rig.trainer.start()
        await rig.trainer.disconnect()

    run(go())


def test_not_permitted_rerequests_and_retries():
    async def go():
        rig = await connected(resend_interval=10)
        rig.results[5] = [5]
        await rig.trainer.set_target_power(150)
        assert rig.client.ops == [0, 7, 5, 0, 5]
        rig.results[5] = [5, 5]
        with pytest.raises(TrainerError, match="not permitted"):
            await rig.trainer.set_target_power(160)
        await rig.trainer.disconnect()

    run(go())


def test_status_control_lost():
    async def go():
        rig = await connected(resend_interval=10)
        await rig.trainer.set_target_power(150)
        rig.client.notify(ftms.MACHINE_STATUS, b"\xff")
        rig.client.notify(ftms.MACHINE_STATUS, b"")
        await until(lambda: rig.client.ops == [0, 7, 5, 0, 5])
        await rig.trainer.pause()
        rig.client.notify(ftms.MACHINE_STATUS, b"\xff")
        await until(lambda: rig.client.ops == [0, 7, 5, 0, 5, 8, 0])
        await asyncio.sleep(0.02)
        assert rig.client.ops[-1] == 0
        await rig.trainer.disconnect()

    run(go())


def test_timeout_and_truncated_response():
    async def go():
        rig = await connected(resend_interval=10)
        rig.silent.add(5)
        task = asyncio.ensure_future(rig.trainer.set_target_power(150))
        await until(lambda: 5 in rig.client.ops)
        rig.client.notify(ftms.CONTROL_POINT, b"\x80")
        rig.client.notify(ftms.CONTROL_POINT, b"\x80\x07\x01")
        with pytest.raises(TrainerError, match="did not respond"):
            await task
        await rig.trainer.disconnect()

    run(go())


def test_commands_serialized():
    async def go():
        rig = await connected()
        await asyncio.gather(*(rig.trainer.set_target_power(100 + i) for i in range(10)))
        assert rig.max_parallel == 1
        await rig.trainer.disconnect()

    run(go())


def test_pause_wins_over_concurrent_start():
    async def go():
        rig = await connected(resend_interval=0.01)
        await rig.trainer.set_target_power(150)
        await asyncio.gather(rig.trainer.start(), rig.trainer.pause())
        n = len(rig.client.writes)
        await asyncio.sleep(0.05)
        assert len(rig.client.writes) == n and rig.client.ops[-1] == 8
        await rig.trainer.disconnect()

    run(go())


def test_telemetry_merge_hr_and_bad_packets():
    async def go():
        rig = Rig(clock=lambda: 1.0)
        rig.has_hr = True
        await rig.trainer.connect("a")
        c = rig.client
        for bad in (b"", b"\x44", b"\x44\x00\x01"):
            c.notify(ftms.BIKE_DATA, bad)
        assert rig.telemetry == []
        c.notify(ftms.BIKE_DATA, bytes.fromhex("41009600"))
        c.notify(ftms.BIKE_DATA, bytes.fromhex("0400b80bb400"))
        assert rig.telemetry[-1] == Telemetry(1.0, 150, 90.0, 30.0, None)
        c.notify(ftms.HR_MEASUREMENT, b"")
        c.notify(ftms.HR_MEASUREMENT, b"\x00\x78")
        c.notify(ftms.BIKE_DATA, bytes.fromhex("4100a000"))
        assert rig.telemetry[-1] == Telemetry(1.0, 160, 90.0, 30.0, 120)

        def boom(_):
            raise ValueError

        rig.trainer.add_listener(boom)
        c.notify(ftms.BIKE_DATA, bytes.fromhex("4100a000"))
        await rig.trainer.disconnect()
        n = len(rig.telemetry)
        c.notify(ftms.BIKE_DATA, bytes.fromhex("4100a000"))
        assert len(rig.telemetry) == n

    run(go())


def test_reconnect_restores_running_target():
    async def go():
        rig = await connected(resend_interval=10)
        await rig.trainer.set_target_power(150)
        rig.fail_connects = 2
        first = rig.client
        first.drop()
        assert rig.states[-1] is S.RECONNECTING
        await rig.trainer.set_target_power(180)  # remembered, no error
        await until(lambda: rig.states[-1] is S.CONNECTED)
        assert len(rig.clients) == 4 and first.disconnects == 1
        assert rig.client.writes == [b"\x00", b"\x07", b"\x05\xb4\x00"]
        await rig.trainer.disconnect()
        assert len(asyncio.all_tasks()) == 1

    run(go())


@pytest.mark.parametrize("halt", ["pause", "stop"])
def test_reconnect_while_halted_does_not_start(halt):
    async def go():
        rig = await connected(resend_interval=0.01)
        await rig.trainer.set_target_power(150)
        await getattr(rig.trainer, halt)()
        rig.client.drop()
        await until(lambda: rig.states[-1] is S.CONNECTED)
        await asyncio.sleep(0.05)
        # the old load may still be applied: halt once, nothing else
        assert rig.client.writes == [b"\x00", b"\x08\x02" if halt == "pause" else b"\x08\x01"]
        await rig.trainer.disconnect()

    run(go())


def test_pause_during_reconnect_handshake():
    async def go():
        rig = await connected(resend_interval=0.01)
        await rig.trainer.set_target_power(150)
        gate = asyncio.Event()
        rig.gate = (0x07, gate)
        rig.client.drop()
        await until(lambda: rig.client.ops == [0, 7])
        await rig.trainer.pause()
        gate.set()
        await until(lambda: rig.states[-1] is S.CONNECTED)
        assert rig.client.ops == [0, 7, 8]
        await rig.trainer.disconnect()

    run(go())


def test_drop_mid_command_and_disconnect_while_reconnecting():
    async def go():
        rig = await connected(resend_interval=10, command_timeout=5)
        rig.silent.add(5)
        task = asyncio.ensure_future(rig.trainer.set_target_power(150))
        await until(lambda: 5 in rig.client.ops)
        rig.hang_connect = True
        rig.client.drop()
        await task  # swallowed: applied after reconnect
        await until(lambda: len(rig.clients) == 2)
        await rig.trainer.disconnect()
        assert rig.states[-2:] == [S.RECONNECTING, S.DISCONNECTED]
        await asyncio.sleep(0.05)
        assert len(rig.clients) == 2 and rig.client.disconnects == 1

    run(go())


def test_drop_during_initial_handshake_fails():
    async def go():
        rig = Rig(command_timeout=5)
        rig.silent.add(0)
        task = asyncio.ensure_future(rig.trainer.connect("a"))
        await until(lambda: rig.clients and rig.client.ops == [0])
        rig.client.drop()
        with pytest.raises(TrainerError, match="lost"):
            await task
        assert rig.states == [S.CONNECTING, S.DISCONNECTED]

    run(go())


@pytest.mark.parametrize("results", [[2], [4, 4]])
def test_refused_halt_falls_back_to_min_target(results):
    async def go():
        rig = await connected(resend_interval=10)
        await rig.trainer.set_target_power(300)
        rig.results[8] = list(results)
        await rig.trainer.pause()
        assert rig.client.writes[-1] == b"\x05\x00\x00"
        await rig.trainer.start()
        rig.results[8], rig.results[5] = [4, 4], [4]
        with pytest.raises(TrainerError, match="may still hold"):
            await rig.trainer.stop()
        await rig.trainer.disconnect()

    run(go())


def test_halt_retried_once():
    async def go():
        rig = await connected(resend_interval=10)
        rig.results[8] = [4]
        await rig.trainer.pause()
        assert rig.client.ops[-2:] == [8, 8]
        n = len(rig.client.writes)
        await rig.trainer.disconnect()
        assert len(rig.client.writes) == n  # already released

    run(go())


def test_failed_start_is_retried_with_target():
    async def go():
        rig = await connected(resend_interval=0.02)
        await rig.trainer.pause()
        await rig.trainer.set_target_power(150)
        rig.results[7] = [4]
        with pytest.raises(TrainerError):
            await rig.trainer.start()
        await until(lambda: rig.client.writes[-2:] == [b"\x07", b"\x05\x96\x00"])
        rig.results[7] = [4]
        with pytest.raises(TrainerError):
            await rig.trainer.start()
        await rig.trainer.pause()
        n = len(rig.client.writes)
        await asyncio.sleep(0.06)
        assert len(rig.client.writes) == n  # pause cancels the retry

    run(go())


def test_foreign_cancelled_error_is_an_error():
    """bleak raises CancelledError when WinRT aborts an operation."""

    async def go():
        rig = await connected(resend_interval=0.01)
        await rig.trainer.set_target_power(150)
        original = rig.client.write_gatt_char

        async def aborted(*args, **kw):
            raise asyncio.CancelledError

        rig.client.write_gatt_char = aborted
        with pytest.raises(TrainerError):
            await rig.trainer.set_target_power(160)
        await asyncio.sleep(0.03)
        rig.client.write_gatt_char = original
        n = len(rig.client.writes)
        await until(lambda: len(rig.client.writes) > n)  # resend loop survived
        rig.client.disconnect = aborted
        rig.client.drop()
        await until(lambda: rig.states[-1] is S.CONNECTED)  # so did reconnecting
        await rig.trainer.disconnect()

    run(go())


OPEN = b"\x11" + ftms.OPEN_MODE_PARAMS


def test_open_mode_is_resent_deferred_and_left():
    async def go():
        assert OPEN == bytes.fromhex("11000000002833")
        rig = await connected(resend_interval=0.02)
        await rig.trainer.set_target_power(150)
        await rig.trainer.set_open_mode()
        n = len(rig.client.writes)
        await until(lambda: len(rig.client.writes) >= n + 2)
        assert set(rig.client.writes[n:]) == {OPEN}  # the old target never comes back
        await rig.trainer.pause()
        n = len(rig.client.writes)
        await rig.trainer.set_open_mode()
        await asyncio.sleep(0.05)
        assert len(rig.client.writes) == n
        await rig.trainer.start()
        assert rig.client.writes[n : n + 2] == [b"\x07", OPEN]
        rig.client.drop()
        await until(lambda: rig.states[-1] is S.CONNECTED)
        assert OPEN in rig.client.writes  # restored after reconnecting
        await rig.trainer.set_target_power(160)
        assert rig.client.writes[-1] == b"\x05\xa0\x00"
        await rig.trainer.set_open_mode()
        await rig.trainer.stop()
        n = len(rig.client.writes)
        await rig.trainer.start()
        await asyncio.sleep(0.05)
        assert OPEN not in rig.client.writes[n:]  # stop forgets it
        await rig.trainer.disconnect()

    run(go())


def test_open_mode_releases_load_on_disconnect():
    async def go():
        rig = await connected(resend_interval=10)
        await rig.trainer.set_open_mode()
        await rig.trainer.disconnect()
        assert rig.client.writes[-1] == b"\x08\x01"

    run(go())


def test_refused_open_mode_falls_back_to_min_target():
    async def go():
        rig = await connected(resend_interval=10)
        await rig.trainer.set_target_power(300)
        rig.results[0x11] = [2]
        with pytest.raises(TrainerError, match="No open mode"):
            await rig.trainer.set_open_mode()
        assert rig.client.writes[-2:] == [OPEN, b"\x05\x00\x00"]
        rig.results[0x11], rig.results[5] = [4], [4]
        with pytest.raises(TrainerError, match="may still hold"):
            await rig.trainer.set_open_mode()
        await rig.trainer.disconnect()

    run(go())
