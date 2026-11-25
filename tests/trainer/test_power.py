import asyncio
from types import SimpleNamespace

import pytest

from pywatts.trainer import ftms
from pywatts.trainer.base import ConnectionState as S
from pywatts.trainer.base import PowerReading, Telemetry, TrainerError
from pywatts.trainer.cycling_power import (
    CP_MEASUREMENT,
    CP_SERVICE,
    CrankCadence,
    PowerMeasurement,
    parse_power_measurement,
)
from pywatts.trainer.fake import METER_FACTOR, FakePowerMeter, FakeTrainer
from pywatts.trainer.ftms import FTMS_SERVICE, HR_SERVICE
from pywatts.trainer.power import PowerMeter
from pywatts.trainer.thread import TrainerBridge


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0000 c800", (200, None, None)),
        ("0000 f6ff", (-10, None, None)),
        ("2000 c800 1000 0004", (200, 16, 1024)),
        ("2300 c800 64 1000 0004", (200, 16, 1024)),  # balance
        ("2f00 c800 64 1111 1000 0004", (200, 16, 1024)),  # + torque
        ("3000 c800 010203040506 1000 0004", (200, 16, 1024)),  # wheel
        ("3f00 c800 64 1111 010203040506 ffff ffff", (200, 65535, 65535)),
        # extremes, angles, dead spots, energy follow the crank data
        ("e01f c800 1000 0004 11111111 22222222 333333 4444 5555 6666", (200, 16, 1024)),
        ("2000 c800 1000 00", (200, None, None)),  # cut off
        ("3000 c800 0102", (200, None, None)),
        ("2000 c8", (None, None, None)),
        ("", (None, None, None)),
    ],
)
def test_parse(raw, expected):
    assert parse_power_measurement(bytes.fromhex(raw.replace(" ", ""))) == PowerMeasurement(
        *expected
    )


def test_cadence():
    crank = CrankCadence()

    def at(now, revs, time):
        return crank.update(PowerMeasurement(100, revs & 0xFFFF, time & 0xFFFF), now)

    assert at(0, 65535, 65000) is None  # one value is not enough
    assert at(1, 65536, 65000 + 683) == pytest.approx(90, abs=0.2)  # both wrap
    assert at(2, 65536, 65000 + 683) == pytest.approx(90, abs=0.2)  # no new event yet
    assert at(4.5, 65536, 65000 + 683) == 0.0  # stopped
    assert at(20, 65537, 65000 + 20000) == 0.0  # the pause is not a revolution
    assert at(21, 65539, 65000 + 20000 + 2048) == 60.0
    assert at(22, 0, 65000 + 20000 + 2049) == 60.0  # counters were reset
    assert crank.update(PowerMeasurement(100), 23) is None


class Client:
    def __init__(self, rig, on_drop):
        self.rig, self.on_drop = rig, on_drop
        self.services = self

    def get_service(self, uuid):
        return object() if uuid == CP_SERVICE and self.rig.has_power else None

    async def connect(self):
        pass

    async def disconnect(self):
        self.on_drop(self)

    async def read_gatt_char(self, _uuid):
        return bytearray(b"\x46")

    async def start_notify(self, uuid, cb):
        assert uuid == CP_MEASUREMENT
        self.cb = cb


class Rig:
    def __init__(self):
        self.has_power, self.now = True, 1.0
        self.clients, self.events = [], []
        self.meter = PowerMeter(self.make, self, clock=lambda: self.now, backoff=(0.01, 0.02))
        self.meter.add_listener(self.events.append)

    def make(self, _device, disconnected_callback):
        self.clients.append(Client(self, disconnected_callback))
        return self.clients[-1]

    async def find_device_by_address(self, addr, timeout):
        return SimpleNamespace(address=addr)

    async def discover(self, timeout, return_adv):
        rows = [
            ("a", "Assioma", -70, [CP_SERVICE.upper()]),
            ("b", None, -50, [CP_SERVICE]),
            ("c", "KICKR CORE", -20, [FTMS_SERVICE, CP_SERVICE]),
            ("d", "TICKR", -10, [HR_SERVICE]),
        ]
        return {
            addr: (
                SimpleNamespace(address=addr, name=None),
                SimpleNamespace(local_name=n, service_uuids=u, service_data={}, rssi=r),
            )
            for addr, n, r, u in rows
        }


def run(coro):
    async def wrapped():
        async with asyncio.timeout(10):
            await coro

    asyncio.run(wrapped())


async def until(cond):
    while not cond():
        await asyncio.sleep(0.002)


def test_scan_leaves_trainers_out():
    async def go():
        assert [(d.addr, d.name) for d in await Rig().meter.scan(0.1)] == [
            ("b", "Power meter"),
            ("a", "Assioma"),
        ]

    run(go())


def test_readings_and_reconnect():
    async def go():
        rig = Rig()
        await rig.meter.connect("a")
        send = rig.clients[0].cb
        send(None, bytearray.fromhex("2000c80010000004"))
        rig.now = 2.0
        send(None, bytearray.fromhex("2000f6ff11000008"))
        send(None, bytearray(b"\x00"))  # unusable
        assert rig.events[2:] == [PowerReading(1.0, 200, None, 70), PowerReading(2.0, 0, 60.0, 70)]
        rig.clients[0].on_drop(None)
        await until(lambda: rig.events[-1] is S.CONNECTED)
        rig.clients[1].cb(None, bytearray.fromhex("2000c80012000010"))
        assert rig.events[-1].cadence_rpm is None  # counters start anew
        n = len(rig.events)
        send(None, bytearray.fromhex("0000c800"))  # old link
        assert len(rig.events) == n
        await rig.meter.disconnect()
        assert len(asyncio.all_tasks()) == 1

    run(go())


def test_not_a_power_meter():
    async def go():
        rig = Rig()
        rig.has_power = False
        with pytest.raises(TrainerError, match="not a power meter"):
            await rig.meter.connect("a")

    run(go())


def test_bridge_meter_is_independent(qtbot):
    made = []

    def trainer():
        made.append(FakeTrainer())
        return made[0]

    def meter():
        return FakePowerMeter(lambda: (200.0, 90.0), period=0.01)

    bridge = TrainerBridge(trainer, pm_factory=meter)
    try:
        with qtbot.waitSignal(bridge.pm_scan_finished) as scan:
            bridge.pm_scan(0.1)
        with qtbot.waitSignal(bridge.pm_reading) as reading:
            bridge.pm_connect(scan.args[0][0].addr)
        assert reading.args[0].watts == round(200 * METER_FACTOR)
        assert bridge.pm_state is S.CONNECTED
        assert bridge.state is S.DISCONNECTED and bridge.hr_state is S.DISCONNECTED
        with qtbot.waitSignal(bridge.hr_error):
            bridge.hr_connect("x")
        bridge.pm_disconnect()
        qtbot.waitUntil(lambda: bridge.pm_state is S.DISCONNECTED)
    finally:
        assert bridge.shutdown()


# --- trainers that are not like a KICKR -------------------------------------------------------


class TrainerRig:
    def __init__(self, features, has_cp=True):
        self.features, self.has_cp, self.now = features, has_cp, 1.0
        self.services = self
        self.callbacks, self.writes = {}, []
        self.result = 1
        self.trainer = ftms.FtmsTrainer(lambda *_a, **_k: self, self, clock=lambda: self.now)
        self.got = []
        self.trainer.add_listener(self.got.append)

    async def find_device_by_address(self, addr, timeout):
        return SimpleNamespace(address=addr)

    async def discover(self, timeout, return_adv):
        rows = [("p", -10, [CP_SERVICE]), ("t", -80, [FTMS_SERVICE]), ("x", -5, [HR_SERVICE])]
        return {
            addr: (
                SimpleNamespace(address=addr, name="n"),
                SimpleNamespace(local_name=None, service_uuids=u, service_data={}, rssi=r),
            )
            for addr, r, u in rows
        }

    def get_service(self, uuid):
        ok = uuid == FTMS_SERVICE or (uuid == CP_SERVICE and self.has_cp)
        return object() if ok else None

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def start_notify(self, char, cb):
        self.callbacks[char] = cb

    async def read_gatt_char(self, char):
        return bytearray(self.features if char == ftms.FEATURES else b"")

    async def write_gatt_char(self, _char, data, response):
        self.writes.append(bytes(data))
        result = self.result if data[0] == ftms.OP_SET_TARGET_POWER else 1
        self.callbacks[ftms.CONTROL_POINT](None, bytearray([0x80, data[0], result]))

    def notify(self, char, raw):
        self.callbacks[char](None, bytearray.fromhex(raw))


FULL = ((1 << 1) | (1 << 14)).to_bytes(4, "little") + (1 << 3).to_bytes(4, "little")
NO_POWER = bytes(8)


def test_power_and_cadence_from_the_cycling_power_service():
    async def go():
        rig = TrainerRig(NO_POWER)
        await rig.trainer.connect("t")  # announces no ERG: not refused
        rig.notify(CP_MEASUREMENT, "2000c80010000004")
        rig.notify(CP_MEASUREMENT, "2000d20011000008")
        rig.notify(ftms.BIKE_DATA, "0000b80b")  # speed only
        assert rig.got[-1] == Telemetry(1.0, 210, 60.0, 30.0, None)
        rig.notify(ftms.BIKE_DATA, "4400b80bb4009600")  # its own values win
        assert rig.got[-1] == Telemetry(1.0, 150, 90.0, 30.0, None)
        await rig.trainer.disconnect()
        await rig.trainer.connect("t")
        rig.notify(CP_MEASUREMENT, "0000c800")
        rig.now = 7.0
        rig.notify(ftms.BIKE_DATA, "0000b80b")
        assert rig.got[-1].power_w is None  # too old
        await rig.trainer.disconnect()

    run(go())


@pytest.mark.parametrize("features, has_cp", [(FULL, True), (NO_POWER, False)])
def test_cycling_power_service_is_not_used(features, has_cp):
    async def go():
        rig = TrainerRig(features, has_cp)
        await rig.trainer.connect("t")
        assert CP_MEASUREMENT not in rig.callbacks
        await rig.trainer.disconnect()

    run(go())


def test_refused_target_says_why():
    async def go():
        rig = TrainerRig(NO_POWER)
        rig.result = ftms.RESULT_NOT_SUPPORTED
        await rig.trainer.connect("t")
        with pytest.raises(TrainerError, match="no ERG"):
            await rig.trainer.set_target_power(150)
        await rig.trainer.disconnect()

    run(go())


def test_scan_lists_possible_trainers_last():
    async def go():
        found = await TrainerRig(FULL).trainer.scan(0.1)
        assert [(d.addr, d.likely) for d in found] == [("t", True), ("p", False)]

    run(go())


@pytest.mark.parametrize(
    "raw, bpm",
    [
        ("16 48 aa03", 72),  # contact, RR interval (Polar H10, Garmin HRM)
        ("10 48 aa03 ab03", 72),  # RR without contact detection
        ("1e 48 3412 aa03", 72),  # + energy
        ("19 4800 3412 aa03", 72),  # 16 bit + energy
        ("14 48 aa03", None),  # not worn
    ],
)
def test_straps_of_other_makers(raw, bpm):
    assert ftms.parse_hr_measurement(bytes.fromhex(raw.replace(" ", ""))) == bpm
