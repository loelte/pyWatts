import math
import sqlite3
from datetime import date

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from pywatts.core.plan import PlanUnit
from pywatts.core.runner import RunnerState
from pywatts.core.workout import Ramp, Sprint, Steady, Workout
from pywatts.storage.db import open_db
from pywatts.storage.plan_repo import PlanRepo
from pywatts.storage.repo import (
    FTP_W,
    HR_ADDR,
    PM_ADDR,
    PM_NAME,
    POWER_MATCH,
    TRAINER_ADDR,
    TRAINER_NAME,
    UNITS,
    Repo,
)
from pywatts.trainer.base import ConnectionState, DeviceInfo, HeartRate, PowerReading, Telemetry
from pywatts.ui.ride import ConnectDialog, HrDialog, PowerMeterDialog, RidePage, mmss
from pywatts.ui.ride_controller import RideController
from pywatts.ui.theme import ZONE_COLORS

UP = ConnectionState.CONNECTED
DOWN = ConnectionState.DISCONNECTED
RETRY = ConnectionState.RECONNECTING


class FakeBridge(QObject):
    telemetry = pyqtSignal(object)
    connection_changed = pyqtSignal(object)
    scan_finished = pyqtSignal(list)
    error = pyqtSignal(str)
    hr_reading = pyqtSignal(object)
    hr_connection_changed = pyqtSignal(object)
    hr_scan_finished = pyqtSignal(list)
    hr_error = pyqtSignal(str)
    pm_reading = pyqtSignal(object)
    pm_connection_changed = pyqtSignal(object)
    pm_scan_finished = pyqtSignal(list)
    pm_error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.state = DOWN
        self.hr_state = DOWN
        self.pm_state = DOWN
        self.calls = []

    def go(self, state):
        self.state = state
        self.connection_changed.emit(state)

    def scan(self, timeout=5.0):
        self.calls.append("scan")

    def connect_trainer(self, addr):
        self.calls.append(("connect", addr))

    def disconnect_trainer(self):
        self.calls.append("disconnect")

    def hr_go(self, state):
        self.hr_state = state
        self.hr_connection_changed.emit(state)

    def hr_scan(self, timeout=5.0):
        self.calls.append("hr_scan")

    def hr_connect(self, addr):
        self.calls.append(("hr_connect", addr))

    def hr_disconnect(self):
        self.calls.append("hr_disconnect")

    def pm_go(self, state):
        self.pm_state = state
        self.pm_connection_changed.emit(state)

    def pm_scan(self, timeout=5.0):
        self.calls.append("pm_scan")

    def pm_connect(self, addr):
        self.calls.append(("pm_connect", addr))

    def pm_disconnect(self):
        self.calls.append("pm_disconnect")

    def set_target_power(self, w):
        self.calls.append(("target", w))

    def set_open_mode(self):
        self.calls.append("open")

    def start(self):
        self.calls.append("start")

    def pause(self):
        self.calls.append("pause")

    def stop(self):
        self.calls.append("stop")


class Env:
    def __init__(self, conn):
        self.now = 1000.0
        self.conn = conn
        self.repo = Repo(conn)
        self.bridge = FakeBridge()
        self.controller = RideController(self.repo, self.bridge, clock=lambda: self.now)
        self.errors = []
        self.finished = []
        self.controller.error.connect(self.errors.append)
        self.controller.session_finished.connect(self.finished.append)

    def ride(self, seconds=60, power=0.5):
        self.bridge.go(UP)
        self.controller.load(Workout("W", [Steady(seconds, power), Steady(60, 1.0)]))
        self.controller.start()

    def tel(self, power=150, age=0.0):
        self.bridge.telemetry.emit(Telemetry(self.now - age, power, 90.0, 32.18688, 140))

    def run(self, seconds, power=150):
        for _ in range(seconds):
            self.now += 1
            if power is not None:
                self.tel(power)
            self.controller.tick()

    def stored(self):
        row = self.repo.list_sessions()[0]
        return row, self.repo.get_samples(row.id)


@pytest.fixture
def env(tmp_path, qtbot):
    conn = open_db(tmp_path / "t.db")
    e = Env(conn)
    yield e
    e.controller.shutdown()
    conn.close()


@pytest.fixture
def page(env, qtbot):
    p = RidePage(env.repo, env.bridge, env.controller)
    qtbot.addWidget(p)
    return p


# --- controller ---
def test_start_requirements(env):
    env.controller.load(Workout("W", [Steady(10, 0.5)]))
    env.controller.start()
    assert len(env.errors) == 1 and not env.controller.is_active
    env.bridge.go(UP)
    env.controller.load(Workout("empty"))
    env.controller.start()
    assert len(env.errors) == 2 and not env.controller.is_active
    assert env.repo.list_sessions() == []


def test_complete_ride_one_sample_per_second_and_restart(env):
    env.repo.set_setting(FTP_W, "300")
    env.bridge.go(UP)
    env.controller.load(Workout("W", [Steady(12, 0.5)]))
    for n in (1, 2):
        env.tel()
        env.controller.start()
        env.run(12)
        assert env.controller.state is RunnerState.DONE
        row, samples = env.stored()
        assert row.status == "completed" and row.ftp_w == 300
        assert [s.t for s in samples] == list(range(12))
        assert all(s.power == 150 and s.target_w == 150 for s in samples)
        assert len(env.finished) == n and env.finished[-1] == row.id
    assert env.finished[0] != env.finished[1]


def test_jittery_ticks_and_late_tick(env):
    env.ride()
    for dt in (0.4, 0.4, 0.4, 3.1, 0.1, 1.0):
        env.now += dt
        env.controller.tick()
    env.controller.stop()
    assert [s.t for s in env.stored()[1]] == list(range(6))


def test_stale_telemetry_is_none(env):
    env.bridge.go(UP)
    env.tel()
    env.ride()
    env.run(8, power=None)
    env.controller.stop()
    powers = [s.power for s in env.stored()[1]]
    assert powers[:6] == [150] * 6
    assert powers[6:9] == [None] * 3
    assert env.controller.latest_telemetry is None
    assert math.isnan(env.controller.history_power[-1])


def test_connection_loss_pauses_and_resume_needs_link(env):
    env.ride()
    env.run(3)
    env.bridge.go(RETRY)
    assert env.controller.state is RunnerState.PAUSED
    assert len(env.errors) == 1
    assert len(env.stored()[1]) == 4  # pause flushed
    env.controller.resume()
    assert env.controller.state is RunnerState.PAUSED and len(env.errors) == 2
    env.bridge.go(UP)
    assert env.controller.state is RunnerState.PAUSED
    env.run(30)
    env.controller.resume()
    assert env.controller.state is RunnerState.RUNNING
    env.run(2)
    env.controller.stop()
    row, samples = env.stored()
    assert row.status == "aborted"
    assert [s.t for s in samples] == list(range(6))


def test_load_refused_while_active(env):
    env.ride()
    assert not env.controller.load(Workout("other", [Steady(1, 1)]))
    assert env.controller.workout.name == "W" and env.errors
    env.controller.pause()
    assert not env.controller.load(Workout("other", [Steady(1, 1)]))


def test_skip_moves_history_position(env):
    env.ride(60)
    env.run(3)
    env.controller.skip()
    env.run(2)
    assert env.controller.history_t[-1] == pytest.approx(62)
    assert any(math.isnan(p) for p in env.controller.history_power)


def test_storage_failure_keeps_riding_and_recovers(env, monkeypatch):
    env.ride()
    real = env.repo.add_samples

    def broken(*_a):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(env.repo, "add_samples", broken)
    env.run(25)
    assert env.controller.state is RunnerState.RUNNING
    assert len(env.errors) == 1 and "disk full" in env.errors[0]
    env.controller.pause()
    monkeypatch.setattr(env.repo, "add_samples", real)
    env.controller.resume()
    env.run(1)
    env.controller.stop()
    assert len(env.stored()[1]) == 27


def test_session_creation_failure_is_retried(env, monkeypatch):
    real = env.repo.start_session
    monkeypatch.setattr(env.repo, "start_session", lambda *_a: 1 / 0)
    env.ride()
    env.run(3)
    assert env.controller.is_active and len(env.errors) == 1
    monkeypatch.setattr(env.repo, "start_session", real)
    env.controller.stop()
    assert len(env.stored()[1]) == 4


def test_finish_failure_retried_on_shutdown(env, monkeypatch):
    env.ride()
    env.run(3)
    real = env.repo.finish_session
    monkeypatch.setattr(env.repo, "finish_session", lambda *_a: 1 / 0)
    env.controller.stop()
    assert env.controller.state is RunnerState.DONE and env.errors and len(env.finished) == 1
    monkeypatch.setattr(env.repo, "finish_session", real)
    env.controller.shutdown()
    assert env.stored()[0].status == "aborted"


def test_pause_or_drop_after_end_creates_no_second_session(env):
    env.ride(10)
    env.run(65)
    env.now += 8  # GUI stalled past the end
    env.bridge.go(RETRY)
    assert env.controller.state is RunnerState.DONE
    assert len(env.repo.list_sessions()) == 1 and env.stored()[0].status == "completed"
    assert env.errors == []


def test_suspend_pauses_without_crediting_time(env):
    env.ride(60)
    env.run(5)
    env.now += 7200
    env.controller.tick()
    assert env.controller.state is RunnerState.PAUSED
    assert env.controller.runner.ride_s == 5 and len(env.errors) == 1
    env.now += 100
    env.controller.resume()
    env.run(5)
    env.controller.stop()
    row, samples = env.stored()
    assert row.status == "aborted" and [s.t for s in samples] == list(
        range(11)
    )  # includes the second being ridden


def test_ride_is_saved_later_if_session_could_not_be_created(env, monkeypatch):
    def broken(*_):
        raise sqlite3.OperationalError("disk full")

    with monkeypatch.context() as m:
        m.setattr(env.repo, "start_session", broken)
        env.ride()
        env.run(5)
        env.controller.stop()
    assert env.repo.list_sessions() == []
    env.controller.shutdown()
    row, samples = env.stored()
    assert row.status == "aborted" and len(samples) == 6


def test_planned_unit_is_linked_also_if_saved_later(env, monkeypatch):
    plans = PlanRepo(env.conn)
    plans.apply("user", "x", [PlanUnit("u", date(2026, 3, 4), "indoor", "T")])

    def broken(*_):
        raise sqlite3.OperationalError("disk full")

    with monkeypatch.context() as m:
        m.setattr(env.repo, "start_session", broken)
        env.bridge.go(UP)
        env.controller.load(Workout("W", [Steady(60, 0.5)]), "u")
        env.controller.start()
        env.run(5)
        env.controller.stop()
    env.controller.load(Workout("other", [Steady(60, 0.5)]))  # must not take the unit along
    env.controller.start()
    assert plans.links()["u"].session_id == env.repo.list_sessions()[-1].id
    assert {s.id: s.workout.name for s in env.repo.list_sessions()}[
        plans.links()["u"].session_id
    ] == "W"


def test_shutdown_aborts(env):
    env.ride()
    env.run(2)
    env.controller.shutdown()
    env.controller.shutdown()
    assert env.stored()[0].status == "aborted" and len(env.finished) == 1


# --- page ---
def test_buttons_follow_state(env, page):
    def enabled():
        buttons = (page.start_button, page.skip_button, page.extend_button, page.stop_button)
        return [b.isEnabled() for b in buttons]

    assert enabled() == [False] * 4
    page.set_workout(Workout("W", [Steady(60, 0.5), Steady(60, 1)]))
    assert enabled() == [False] * 4
    env.bridge.go(UP)
    assert enabled() == [True, False, False, False]
    page.start_button.click()
    assert enabled() == [True] * 4 and page.start_button.text() == "Pause"
    env.bridge.go(RETRY)
    assert page.start_button.text() == "Resume" and not page.start_button.isEnabled()
    assert "lost" in page.error_label.text() and not page.error_label.isHidden()
    env.bridge.go(UP)
    assert page.error_label.isHidden()
    page.start_button.click()
    assert env.controller.state is RunnerState.RUNNING
    page.confirm_stop = lambda: False
    page.stop_button.click()
    assert env.controller.is_active
    page.confirm_stop = lambda: True
    page.stop_button.click()
    assert enabled() == [True, False, False, False] and page.start_button.text() == "Start"


def test_display_extend_and_units(env, page):
    page.set_workout(Workout("W", [Steady(60, 0.5), Steady(60, 1)]))
    env.bridge.go(UP)
    page.start_button.click()
    env.run(6)
    assert page.power_label.text() == "150 W"
    assert page.cadence_label.text() == "90 rpm"
    assert page.hr_label.text() == "140 bpm"
    assert page.target_label.text() == "100 W"
    assert page.time_left_label.text() == "01:54"
    assert page.block_left_label.text() == "00:54"
    assert page.speed_label.text() == "32.2 km/h"
    page.extend_button.click()
    assert page._drawn[0][-1].t1 == 150
    assert page.time_left_label.text() == "02:24"
    env.repo.set_setting(UNITS, "imperial")
    page.reload_settings()
    assert page.speed_label.text() == "20.0 mph"
    env.run(10, power=None)
    assert page.power_label.text() == "--"


def test_connect_dialog(env, qtbot, page):
    dialog = ConnectDialog(env.repo, env.bridge)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.reconnect_button.isHidden()
    dialog.scan_button.click()
    assert not dialog.scan_button.isEnabled()
    env.bridge.error.emit("no bluetooth")
    assert dialog.scan_button.isEnabled() and dialog.error_label.text() == "no bluetooth"
    dialog.scan_button.click()
    env.bridge.scan_finished.emit([DeviceInfo("far", "A", -90), DeviceInfo("near", "B", -40)])
    assert dialog.device_list.item(0).text() == "near  (-40 dBm)"
    dialog.connect_button.click()
    assert env.bridge.calls[-1] == ("connect", "B")
    assert env.repo.get_setting(TRAINER_ADDR) == "B"
    assert env.repo.get_setting(TRAINER_NAME) == "near"
    env.bridge.go(UP)
    assert not dialog.isVisible()

    assert page.connect_button.text() == "Disconnect"
    page.connect_button.click()
    assert env.bridge.calls[-1] == "disconnect"
    env.bridge.go(DOWN)
    opened = []
    page.open_connect_dialog = lambda: opened.append(1)
    page.connect_button.click()
    assert opened
    assert page.reconnect_button.text() == "Reconnect near"
    page.reconnect_button.click()
    assert env.bridge.calls[-1] == ("connect", "B")


def test_bridge_error_shown(env, page):
    env.bridge.error.emit("boom")
    assert page.error_label.text() == "boom"
    env.ride()
    env.bridge.go(RETRY)
    env.bridge.error.emit("not connected")
    assert "Ride paused" in page.error_label.text() and "not connected" in page.error_label.text()


def test_mmss():
    assert [mmss(0), mmss(65.9), mmss(3600), mmss(-1)] == ["00:00", "01:05", "60:00", "00:00"]


# --- heart rate strap ---
def test_strap_overrides_trainer_and_is_recorded(env, page):
    env.repo.set_setting("max_hr", "200")
    page.reload_settings()
    env.ride()
    env.tel()
    assert page.hr_label.text() == "140 bpm"
    env.bridge.hr_go(UP)
    env.bridge.hr_reading.emit(HeartRate(env.now, 165, 80))
    assert page.hr_label.text() == "165 bpm" and "Z4" in page.hr_caption.text()
    assert ZONE_COLORS[4] in page.hr_label.styleSheet()
    assert "80 %" in page.hr_status_label.text()
    env.run(1)
    env.bridge.hr_reading.emit(HeartRate(env.now, None, 80))  # no skin contact
    assert page.hr_label.text() == "140 bpm"
    env.bridge.hr_reading.emit(HeartRate(env.now, 190, 80))
    env.bridge.hr_go(RETRY)  # a lost strap does not pause the ride
    assert page.hr_label.text() == "140 bpm"
    assert env.controller.state is RunnerState.RUNNING
    env.controller.stop()
    assert 165 in [s.hr for s in env.stored()[1]]


def test_strap_without_trainer_and_stale(env, page):
    env.bridge.hr_go(UP)
    env.bridge.hr_reading.emit(HeartRate(env.now, 120))
    assert page.hr_label.text() == "120 bpm"
    assert "background" not in page.hr_label.styleSheet()  # zones unknown
    env.now += 6
    env.controller.tick()
    assert page.hr_label.text() == "--"


def test_hr_dialog_and_buttons(env, page, qtbot):
    dialog = HrDialog(env.repo, env.bridge)
    qtbot.addWidget(dialog)
    dialog.scan()
    env.bridge.scan_finished.emit([DeviceInfo("trainer", "T", -10)])
    assert dialog.device_list.count() == 0
    env.bridge.hr_scan_finished.emit([DeviceInfo("TICKR", "H", -70), DeviceInfo("x", "X", -30)])
    assert dialog.device_list.currentItem().text().startswith("TICKR")
    dialog.connect_selected()
    assert env.bridge.calls == ["hr_scan", ("hr_connect", "H")]
    assert env.repo.get_setting(HR_ADDR) == "H" and env.repo.get_setting(TRAINER_ADDR) == ""
    page.reload_settings()
    page.hr_reconnect_button.click()
    env.bridge.hr_go(UP)
    page.hr_button.click()
    assert env.bridge.calls[2:] == [("hr_connect", "H"), "hr_disconnect"]


def test_segments_count_pauses(env):
    env.ride()
    env.run(3)
    env.controller.pause()
    env.controller.resume()
    env.run(3)
    env.controller.stop()
    assert {s.segment for s in env.stored()[1]} == {0, 1}


def test_sprint_and_countdown(env, page):
    env.repo.set_setting(FTP_W, "200")
    page.reload_settings()
    env.bridge.go(UP)
    segments = [Steady(20, 0.5), Sprint(15), Steady(20, 0.5), Steady(20, 0.7), Ramp(5, 0.5, 0.8)]
    env.controller.load(Workout("W", segments))
    env.controller.start()
    label = page.countdown_label
    env.run(9)
    assert label.isHidden()
    env.run(1)
    assert not label.isHidden() and "SPRINT" in label.text() and ">10<" in label.text()
    env.controller.pause()
    assert label.isHidden()
    env.controller.resume()
    env.run(9)
    assert ">1<" in label.text()
    env.run(1, power=900)
    assert label.isHidden() and env.bridge.calls[-1] == "open"
    assert page.target_label.text() == "Open"
    assert not page.power_up_button.isEnabled()
    env.run(14, power=900)  # next is Z1
    assert label.isHidden()
    assert page.plot.viewRange()[1][1] > 900
    env.run(1)
    assert env.bridge.calls[-1] == ("target", 100) and page.power_up_button.isEnabled()
    env.run(15)  # next is Z2
    assert label.isHidden()
    env.run(15)  # next is a ramp that ends in Z3
    assert "Z3" in label.text() and not label.isHidden()
    env.controller.stop()
    assert [s.target_w for s in env.stored()[1]][20:35] == [0] * 15


# --- power meter --------------------------------------------------------------------------------


def meter(env, watts, cadence=80.0, age=0.0):
    env.bridge.pm_reading.emit(PowerReading(env.now - age, watts, cadence, 70))


def test_meter_wins_and_falls_back(env):
    meter(env, 120)  # before the trainer is connected
    assert env.controller.latest_telemetry.power_w == 120
    assert env.controller.latest_telemetry.speed_kph is None
    env.bridge.pm_go(UP)
    env.ride()
    for _ in range(5):
        env.now += 1
        env.tel(150)
        meter(env, 144)
        env.controller.tick()
    tel = env.controller.latest_telemetry
    assert (tel.power_w, tel.cadence_rpm, tel.hr_bpm) == (144, 80.0, 140)
    meter(env, 144, cadence=None)
    assert env.controller.latest_telemetry.cadence_rpm == 90.0  # the trainer's
    env.run(10, power=150)  # no readings from the meter
    assert env.controller.latest_telemetry.power_w == 150
    env.controller.stop()
    powers = [s.power for s in env.repo.get_samples(env.finished[0])]
    assert powers[1:6] == [144] * 5 and powers[-1] == 150


def test_losing_the_meter_does_not_pause(env):
    env.bridge.pm_go(UP)
    env.ride()
    env.run(2)
    meter(env, 144)
    env.bridge.pm_go(RETRY)
    assert env.controller.state is RunnerState.RUNNING and env.errors == []
    assert env.controller.latest_telemetry.power_w == 150


def test_meter_controls(env, qtbot):
    page = RidePage(env.repo, env.bridge, env.controller)
    qtbot.addWidget(page)
    opened = []
    page.open_power_meter_dialog = lambda: opened.append(1)
    assert page.pm_button.text() == "Power meter" and page.pm_status_label.isHidden()
    page.pm_button.click()
    assert opened == [1]
    env.bridge.pm_go(UP)
    meter(env, 200)
    assert page.pm_status_label.text() == "Meter 70 %"
    assert page.power_label.text() == "200 W"
    env.bridge.pm_go(RETRY)
    assert page.pm_status_label.text() == "Meter reconnecting"
    assert page.pm_button.accessibleName() == "Disconnect power meter"
    page.pm_button.click()
    assert env.bridge.calls[-1] == "pm_disconnect"
    env.bridge.pm_error.emit("no meter")
    assert "no meter" in page.error_label.text()


def test_meter_dialog(env, qtbot):
    dialog = PowerMeterDialog(env.repo, env.bridge)
    qtbot.addWidget(dialog)
    dialog.scan()
    env.bridge.pm_scan_finished.emit([DeviceInfo("far", "a", -80), DeviceInfo("near", "b", -40)])
    dialog.connect_selected()
    assert env.bridge.calls == ["pm_scan", ("pm_connect", "b")]
    assert (env.repo.get_setting(PM_ADDR), env.repo.get_setting(PM_NAME)) == ("b", "near")
    assert env.repo.get_setting(TRAINER_ADDR) == ""


def test_doubtful_trainers_come_last(env, qtbot):
    dialog = ConnectDialog(env.repo, env.bridge)
    qtbot.addWidget(dialog)
    dialog.scan()
    env.bridge.scan_finished.emit(
        [DeviceInfo("meter", "a", -20, likely=False), DeviceInfo("trainer", "b", -80)]
    )
    texts = [dialog.device_list.item(i).text() for i in range(2)]
    assert texts[0].startswith("trainer") and "perhaps" in texts[1]


def match_ride(env, seconds, factor=0.96):
    for _ in range(seconds):
        env.now += 1
        told = [c[1] for c in env.bridge.calls if isinstance(c, tuple) and c[0] == "target"][-1]
        env.tel(told)
        meter(env, round(told * factor))
        env.controller.tick()


def test_matching(env, qtbot):
    page = RidePage(env.repo, env.bridge, env.controller)
    qtbot.addWidget(page)
    env.repo.set_setting(FTP_W, "250")
    env.repo.set_setting(POWER_MATCH, "1")
    env.bridge.pm_go(UP)
    env.ride(600, 1.0)
    match_ride(env, 120)
    assert env.controller.latest_telemetry.power_w == pytest.approx(250, abs=3)
    offset = env.controller.match_offset_w
    assert offset > 5 and page.pm_status_label.text() == f"Meter 70 %  ·  +{offset} W"
    assert env.controller.runner.target_w == 250  # the plan stays the plan
    env.controller.pause()
    n = len(env.bridge.calls)
    for _ in range(100):
        env.now += 1
        meter(env, 100)
        env.controller.tick()
    assert len(env.bridge.calls) == n  # nothing is written while paused
    env.controller.resume()
    env.repo.set_setting(POWER_MATCH, "")
    match_ride(env, 10)
    env.controller.reload_settings()
    assert env.bridge.calls[-1] == ("target", 250) and env.controller.match_offset_w is None
    env.controller.stop()
    assert {s.target_w for s in env.repo.get_samples(env.finished[0])[20:100]} == {250}


def test_matching_is_off_by_default(env):
    env.bridge.pm_go(UP)
    env.ride(600, 0.5)
    match_ride(env, 120)
    assert [c for c in env.bridge.calls if isinstance(c, tuple)] == [("target", 100)]
    assert env.controller.match_offset_w is None
