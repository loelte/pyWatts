import pytest

from pywatts.core.power_match import MAX_W, STEP_S, STEP_W, PowerMatch


class Trainer:
    def __init__(self):
        self.calls = []

    @property
    def target(self):
        return next(c for c in reversed(self.calls) if isinstance(c, int))

    def set_target_power(self, w):
        self.calls.append(w)

    def set_open_mode(self):
        self.calls.append("open")

    def start(self):
        self.calls.append("start")

    def pause(self):
        self.calls.append("pause")

    def stop(self):
        self.calls.append("stop")


class Env:
    def __init__(self, enabled=True):
        self.now = 100.0
        self.trainer = Trainer()
        self.match = PowerMatch(self.trainer, lambda: self.now)
        self.match.enabled = enabled
        self.factor, self.shift = 0.96, 0

    def start(self, w=200):
        self.match.start()
        self.match.set_target_power(w)

    def meter(self):
        return round(self.trainer.target * self.factor) + self.shift

    def ride(self, seconds, trainer=True, meter=True):
        for _ in range(seconds):
            self.now += 1
            self.match.update(
                self.trainer.target if trainer else None, self.meter() if meter else None
            )


@pytest.mark.parametrize("factor", [0.96, 1.05, 0.9])
def test_converges(factor):
    env = Env()
    env.factor = factor
    env.start(250)
    env.ride(120)
    assert env.meter() == pytest.approx(250, abs=3)
    n = len(env.trainer.calls)
    env.ride(120)
    assert len(env.trainer.calls) <= n + 1  # it stays there


def test_waits_then_moves_in_small_steps():
    env = Env()
    env.factor = 0.85
    env.start(200)
    env.ride(17)
    assert env.trainer.calls == ["start", 200]
    times = []
    for _ in range(60):
        n = len(env.trainer.calls)
        env.ride(1)
        if len(env.trainer.calls) > n:
            times.append(env.now)
            assert abs(env.trainer.calls[-1] - env.trainer.calls[-2]) <= STEP_W
    assert len(times) > 3
    assert all(b - a >= STEP_S for a, b in zip(times, times[1:], strict=False))


@pytest.mark.parametrize(
    "target, factor, limit", [(200, 0.5, 40), (400, 0.5, MAX_W), (200, 1.9, -40)]
)
def test_limited(target, factor, limit):
    env = Env()
    env.factor = factor
    env.start(target)
    env.ride(300)
    assert env.match.offset_w == limit and env.trainer.target == target + limit
    env.match.set_target_power(60)  # the limit of the new target counts at once
    assert env.trainer.target == 60 + (12 if limit > 0 else -12)
    env.match.set_target_power(0)
    assert env.trainer.target == 0


def test_offset_is_kept_over_target_changes_but_frozen():
    env = Env()
    env.start(200)
    env.ride(100)
    offset = env.match.offset_w
    assert offset == pytest.approx(8, abs=2)
    env.factor = 0.8
    for w in range(201, 260):  # a ramp never settles
        env.match.set_target_power(w)
        env.ride(1)
    assert env.match.offset_w == offset and env.trainer.target == 259 + offset


def test_meter_lost_steps_back():
    env = Env()
    env.start(250)
    env.ride(120)
    assert env.match.offset_w > 5
    n = len(env.trainer.calls)
    env.ride(29, meter=False)
    assert len(env.trainer.calls) == n
    env.ride(30, meter=False)
    assert env.match.offset_w == 0 and env.trainer.target == 250


def test_coasting_and_missing_trainer_power_change_nothing():
    env = Env()
    env.start(250)
    env.ride(120)
    n = len(env.trainer.calls)
    env.ride(100, trainer=False)
    for _ in range(60):
        env.now += 1
        env.match.update(20, 0)
    assert len(env.trainer.calls) == n


def test_nothing_is_sent_while_paused_stopped_or_open():
    env = Env()
    env.start(250)
    env.ride(30)
    for command in (env.match.pause, env.match.set_open_mode, env.match.stop):
        command()
        n = len(env.trainer.calls)
        env.factor = 0.8
        env.ride(100)
        env.ride(100, meter=False)
        assert len(env.trainer.calls) == n
        env.match.enabled = False
        env.match.enabled = True
        assert len(env.trainer.calls) == n
        env.match.start()
        env.match.set_target_power(250)
        assert env.trainer.calls[-2:] == ["start", 250]
        env.factor = 0.96
        env.ride(60)
        assert env.match.offset_w > 0


def test_disabled_passes_through():
    env = Env(enabled=False)
    env.start(200)
    env.ride(100)
    env.match.set_open_mode()
    env.match.set_target_power(150)
    env.match.pause()
    env.match.stop()
    assert env.trainer.calls == ["start", 200, "open", 150, "pause", "stop"]
    assert env.match.offset_w == 0


def test_switching_off_restores_the_target_at_once():
    env = Env()
    env.start(250)
    env.ride(120)
    assert env.trainer.target > 250
    env.match.enabled = False
    assert env.trainer.target == 250 and env.match.offset_w == 0


def test_without_meter():
    env = Env()
    env.start(250)
    env.ride(300, meter=False)
    assert env.trainer.calls == ["start", 250]
