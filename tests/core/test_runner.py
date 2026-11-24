import pytest

from pywatts.core.runner import Runner, RunnerState
from pywatts.core.workout import Ramp, Sprint, Steady, Workout

IDLE, RUNNING, PAUSED, DONE = RunnerState


class Rig:
    """Runner with fake clock, fake trainer and an event log."""

    def __init__(self, *segments):
        self.now = 1000.0
        self.calls = []
        self.log = []
        self.r = Runner(Workout("w", list(segments)), 200, self, lambda: self.now)
        for name in ("target_changed", "block_changed", "state_changed", "finished"):
            getattr(self.r, name).append(lambda arg, name=name: self.log.append((name, arg)))

    def set_target_power(self, w):
        assert isinstance(w, int)
        self.calls.append(w)

    def set_open_mode(self):
        self.calls.append("open")

    def start(self):
        self.calls.append("start")

    def pause(self):
        self.calls.append("pause")

    def stop(self):
        self.calls.append("stop")

    def tick(self, dt=1.0, n=1):
        for _ in range(n):
            self.now += dt
            self.r.tick()

    def events(self, name):
        return [arg for n, arg in self.log if n == name]

    def take(self):
        calls, self.calls = self.calls, []
        return calls


@pytest.fixture
def rig():
    """Three blocks: 100 W for 10 s, 200 W for 20 s, 150 W for 30 s."""
    return Rig(Steady(10, 0.5), Steady(20, 1.0), Steady(30, 0.75))


def test_idle(rig):
    r = rig.r
    assert (r.state, r.target_w, r.elapsed_s, r.ride_s) == (IDLE, 0, 0, 0)
    assert (r.remaining_s, r.block_remaining_s) == (60, 10)
    for command in (r.pause, r.resume, r.skip_block, r.extend, r.stop, r.tick):
        rig.now += 1
        command()
    assert rig.calls == rig.log == []
    assert r.remaining_s == 60


def test_empty_workout():
    rig = Rig()
    assert (rig.r.remaining_s, rig.r.block_remaining_s) == (0, 0)
    with pytest.raises(ValueError):
        rig.r.start()
    assert rig.r.state is IDLE
    assert rig.calls == rig.log == []


def test_full_ride_sends_on_change_only(rig):
    rig.r.start()
    assert rig.calls == ["start", 100]
    rig.tick(n=59)
    assert rig.calls == ["start", 100, 200, 150]
    assert (rig.r.remaining_s, rig.r.block_remaining_s) == (1, 1)
    rig.tick(n=10)
    assert rig.calls == ["start", 100, 200, 150, "stop"]
    assert rig.events("block_changed") == [1, 2]
    assert rig.events("target_changed") == [100, 200, 150, 0]
    assert rig.events("state_changed") == [RUNNING, DONE]
    assert rig.events("finished") == [True]
    assert (rig.r.target_w, rig.r.elapsed_s, rig.r.ride_s) == (0, 60, 60)


def test_zero_watt_target_is_sent_after_start():
    rig = Rig(Steady(10, 0))
    rig.r.start()
    assert rig.calls == ["start", 0]


def test_ramp_per_second():
    rig = Rig(Ramp(10, 0.5, 1.0))
    rig.r.start()
    rig.tick(n=9)
    assert rig.calls == ["start", 100, 110, 120, 130, 140, 150, 160, 170, 180, 190]


def test_pause_resume(rig):
    r = rig.r
    r.start()
    rig.tick(n=3)
    rig.now += 0.5
    r.pause()
    assert (r.state, r.elapsed_s, r.ride_s, r.target_w) == (PAUSED, 3.5, 3.5, 100)
    rig.take()
    rig.tick(n=100)
    assert (r.elapsed_s, r.ride_s) == (3.5, 3.5)
    r.pause()
    r.start()
    assert rig.calls == []
    rig.now += 50
    r.resume()
    r.resume()
    assert rig.take() == ["start", 50]
    rig.tick(n=4)
    assert rig.take() == [60, 70, 80, 90]
    rig.tick()
    assert rig.take() == [100]
    assert r.elapsed_s == r.ride_s == 8.5
    rig.tick()
    assert rig.take() == []
    rig.tick()
    assert rig.take() == [200]


def test_ramp_in_late_tick_ends_cleanly(rig):
    rig.r.start()
    rig.r.pause()
    rig.r.resume()
    rig.take()
    rig.tick(7.3)
    assert rig.take() == [100]


def test_ramp_in_combines_with_ramp_and_block_change():
    rig = Rig(Steady(2, 1.0), Ramp(10, 0.5, 1.0))
    rig.r.start()
    rig.r.pause()
    rig.r.resume()
    rig.tick(n=6)
    assert rig.calls == ["start", 200, "pause", "start", 100, 120, 70, 88, 108, 130, 140]


def test_pause_during_ramp_in_restarts_it(rig):
    r = rig.r
    r.start()
    r.pause()
    r.resume()
    rig.tick(n=2)
    r.pause()
    assert r.target_w == 70
    rig.now += 10
    r.resume()
    assert rig.take()[-2:] == ["start", 50]
    rig.tick(n=5)
    assert rig.take() == [60, 70, 80, 90, 100]


def test_resume_always_sends():
    rig = Rig(Steady(60, 0))
    rig.r.start()
    rig.r.pause()
    rig.r.resume()
    assert rig.calls == ["start", 0, "pause", "start", 0]


def test_skip(rig):
    r = rig.r
    r.start()
    rig.tick(n=4)
    r.skip_block()
    assert (r.block_index, r.elapsed_s, r.ride_s, r.target_w) == (1, 10, 4, 200)
    assert (r.remaining_s, r.block_remaining_s) == (50, 20)
    rig.tick()
    assert (r.elapsed_s, r.ride_s) == (11, 5)
    r.skip_block()
    r.skip_block()
    r.skip_block()
    assert rig.calls == ["start", 100, 200, 150, "stop"]
    assert r.state is DONE
    assert (r.elapsed_s, r.remaining_s, r.ride_s) == (60, 0, 5)
    assert rig.events("finished") == [True]


def test_skip_while_paused(rig):
    r = rig.r
    r.start()
    r.pause()
    rig.take()
    r.skip_block()
    assert (r.state, r.block_index, r.elapsed_s, r.target_w) == (PAUSED, 1, 10, 200)
    assert rig.calls == []
    rig.now += 9
    r.resume()
    assert rig.take() == ["start", 100]
    rig.tick()
    assert (r.elapsed_s, r.ride_s) == (11, 1)
    r.pause()
    r.skip_block()
    r.skip_block()
    assert r.state is DONE
    assert rig.events("finished") == [True]
    assert rig.calls[-1] == "stop"


def test_extend(rig):
    r = rig.r
    r.start()
    rig.tick(n=5)
    r.extend()
    r.extend(5)
    assert [(b.t0, b.t1) for b in r.blocks] == [(0, 45), (45, 65), (65, 95)]
    assert (r.remaining_s, r.block_remaining_s) == (90, 40)
    r.extend(0)
    r.extend(-5)
    assert r.remaining_s == 90
    rig.tick(n=39)
    assert r.block_index == 0
    rig.tick()
    assert r.block_index == 1
    rig.tick(50)
    assert rig.events("finished") == [True]
    assert r.ride_s == 95


def test_extend_ramp_and_paused():
    rig = Rig(Ramp(10, 0.5, 1.0))
    rig.r.start()
    rig.tick(5)
    rig.r.extend(10)
    assert rig.calls == ["start", 100, 150, 125]
    rig.r.pause()
    rig.r.extend(5)
    assert rig.r.remaining_s == 20
    assert rig.calls[-1] == "pause"


def test_late_tick_crosses_blocks(rig):
    rig.r.start()
    rig.tick(35)
    assert (rig.r.block_index, rig.r.elapsed_s) == (2, 35)
    assert rig.calls == ["start", 100, 150]
    assert rig.events("block_changed") == [2]


@pytest.mark.parametrize("method", ["tick", "pause", "skip_block", "extend", "stop"])
def test_late_past_end(rig, method):
    rig.r.start()
    rig.now += 500
    getattr(rig.r, method)()
    assert (rig.r.state, rig.r.ride_s, rig.r.elapsed_s, rig.r.block_index) == (DONE, 60, 60, 2)
    assert rig.calls == ["start", 100, "stop"]
    assert rig.events("finished") == [True]


@pytest.mark.parametrize("paused", [False, True])
def test_stop_and_done_commands(rig, paused):
    r = rig.r
    r.start()
    rig.tick(n=3)
    if paused:
        r.pause()
    rig.take()
    r.stop()
    assert rig.take() == ["stop"]
    assert (r.state, r.target_w, r.ride_s) == (DONE, 0, 3)
    log = list(rig.log)
    for c in (r.start, r.pause, r.resume, r.skip_block, r.extend, r.stop, r.tick):
        rig.now += 100
        c()
    assert rig.calls == []
    assert rig.log == log
    assert rig.events("finished") == [False]
    assert r.ride_s == 3


def test_clock_going_backwards(rig):
    rig.r.start()
    rig.tick(5)
    rig.tick(-3)
    assert rig.r.ride_s == 5


EVENTS = ["target_changed", "block_changed", "state_changed", "finished"]
METHODS = ["start", "pause", "resume", "skip_block", "extend", "stop", "tick"]


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("method", METHODS)
def test_reentrant_call(rig, event, method):
    """Any listener may call any method without corrupting order or finished."""
    fired = []

    def listener(_):
        if not fired:
            fired.append(True)
            getattr(rig.r, method)()

    getattr(rig.r, event).insert(0, listener)
    rig.r.start()
    rig.tick(n=200)
    rig.r.resume()
    rig.tick(n=200)
    assert fired
    assert rig.r.state is DONE
    assert rig.events("finished") == [method != "stop" or event == "finished"]
    assert rig.calls.count("stop") == 1
    assert rig.calls[-1] == "stop"
    assert rig.log[-1][0] == "finished"
    assert rig.events("state_changed")[0] is RUNNING
    assert rig.events("state_changed")[-1] is DONE
    assert rig.events("target_changed")[-1] == 0


def test_reentrant_order(rig):
    def on_block(i):
        if i == 1:
            rig.r.skip_block()

    rig.r.block_changed.insert(0, on_block)
    rig.r.start()
    rig.tick(10)
    assert rig.events("block_changed") == [1, 2]
    assert rig.events("target_changed") == [100, 200, 150]
    assert rig.calls == ["start", 100, 200, 150]


def test_listener_removing_itself_and_raising(rig):
    def bad(_):
        rig.r.state_changed.remove(bad)
        raise RuntimeError("boom")

    rig.r.state_changed.insert(0, bad)
    with pytest.raises(RuntimeError):
        rig.r.start()
    assert rig.events("state_changed") == [RUNNING]
    rig.tick(100)
    assert rig.events("finished") == [True]


def test_sprint_is_open_mode():
    rig = Rig(Steady(10, 0.5), Sprint(5), Steady(10, 0.5))
    rig.r.start()
    assert rig.r.next_block.open and not rig.r.open_mode
    rig.tick(n=10)
    assert rig.take() == ["start", 100, "open"]
    assert rig.r.open_mode and rig.r.target_w == 0
    rig.r.adjust_power(10)
    assert rig.take() == []  # kept for the next ERG block
    rig.r.pause()
    rig.r.resume()
    assert rig.take() == ["pause", "start", "open"]
    rig.tick(n=4)
    assert rig.take() == []
    rig.tick()
    assert not rig.r.open_mode and rig.r.next_block is None
    rig.tick()
    assert rig.take()[-1] == 110
