import asyncio

import pytest

from pywatts.trainer.base import ConnectionState, Telemetry, TrainerError
from pywatts.trainer.fake import FakeTrainer


def ride(seed, samples=60):
    async def go():
        t = FakeTrainer(time_scale=2000, seed=seed)
        events = []
        t.add_listener(events.append)
        (device,) = await t.scan()
        with pytest.raises(TrainerError):
            await t.start()
        await t.connect(device.addr)
        await t.start()
        await t.set_target_power(200)
        while len(events) < samples:
            await asyncio.sleep(0.001)
        await t.pause()
        n = len(events)
        while len(events) < n + 30:
            await asyncio.sleep(0.001)
        await t.disconnect()
        assert events[-1] is ConnectionState.DISCONNECTED
        return n, [e for e in events if isinstance(e, Telemetry)]

    return asyncio.run(go())


def test_simulation():
    n, samples = ride(1)
    assert samples[0].power_w < 130  # lag
    steady = samples[20 : n - 3]
    assert 190 < sum(s.power_w for s in steady) / len(steady) < 210
    assert len({s.power_w for s in steady}) > 3
    assert 85 < sum(s.cadence_rpm for s in steady) / len(steady) < 95
    assert 90 < steady[-1].hr_bpm < 190
    assert samples[-1].power_w < 5 and samples[-1].cadence_rpm < 5


def test_seed_is_deterministic():
    a, b = FakeTrainer(seed=3), FakeTrainer(seed=3)
    for t in (a, b):
        t._running, t._target = True, 200
    key = [(x.power_w, x.cadence_rpm, x.hr_bpm) for x in (a._step() for _ in range(20))]
    assert key == [(x.power_w, x.cadence_rpm, x.hr_bpm) for x in (b._step() for _ in range(20))]


def test_cancelled_connect():
    async def go():
        t = FakeTrainer()
        events = []
        t.add_listener(events.append)
        task = asyncio.ensure_future(t.connect("x"))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events == [ConnectionState.CONNECTING, ConnectionState.DISCONNECTED]

    asyncio.run(go())
