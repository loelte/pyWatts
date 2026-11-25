import sqlite3

import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from pywatts import app, platform
from pywatts.storage.repo import NAME, Repo
from pywatts.storage.riders import Riders
from pywatts.ui.main_window import LIBRARY, PROFILE, MainWindow
from pywatts.ui.riders import RiderPicker


@pytest.fixture
def data(tmp_path, monkeypatch):
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setattr(platform, "data_dir", lambda: path)
    return path


def test_only_the_default_database_uses_the_first_login(data, tmp_path):
    assert app.strava_account(platform.db_path(), "id") == ""
    assert app.strava_account(data / "x" / ".." / "pywatts.db", "id") == ""
    assert app.strava_account(data / "riders" / "pywatts.db", "id") == "id"
    assert app.strava_account(tmp_path / "pywatts.db", "id") == "id"


def test_one_real_app_whatever_the_database(data, tmp_path):
    first = app.lock_all(platform.db_path(), fake=False)
    assert isinstance(first, list)
    assert app.lock_all(tmp_path / "new" / "other.db", fake=False) == "pyWatts is already running."
    simulated = app.lock_all(app.default_db(True), fake=True)  # needs no trainer
    assert isinstance(simulated, list)
    app.unlock_all(first)
    app.unlock_all(simulated)


def test_database_is_locked_too(data, tmp_path):
    db = tmp_path / "other.db"
    first = app.lock_all(db, fake=True)
    assert isinstance(first, list)
    assert "other.db" in app.lock_all(db, fake=False)
    # What it got before failing was given back.
    again = app.lock_all(platform.db_path(), fake=False)
    assert isinstance(again, list)
    app.unlock_all(first)
    app.unlock_all(again)


class Script:
    """Does one step per window that is shown."""

    def __init__(self, monkeypatch, *steps):
        self.steps, self.seen, self.errors = list(steps), [], []
        real = MainWindow.show

        def show(window):
            real(window)
            page = window.sidebar.currentRow()
            self.seen.append((window.riders.current.name, window.windowTitle(), page))
            step = self.steps.pop(0)
            QTimer.singleShot(300, lambda: step(window))

        monkeypatch.setattr(MainWindow, "show", show)
        monkeypatch.setattr(QMessageBox, "critical", lambda _p, _t, text: self.errors.append(text))


def add(name):
    def step(window):
        window.riders.ask_name = lambda: name
        window.riders._guard(window.riders._add)

    return step


def switch(name):
    def step(window):
        riders = window.riders._riders.list()
        window.riders._switch(next(r for r in riders if r.name == name))

    return step


def test_riders_take_turns(data, qapp, monkeypatch):
    riders = Riders(data / "t.db")
    script = Script(monkeypatch, add("Bob"), switch("Rider 1"), switch("Bob"), lambda w: w.close())
    assert app.run_riders(qapp, riders, fake=True) == 0
    assert script.seen == [
        ("Rider 1", "pyWatts", LIBRARY),
        ("Bob", "pyWatts – Bob", PROFILE),
        ("Rider 1", "pyWatts – Rider 1", LIBRARY),
        ("Bob", "pyWatts – Bob", LIBRARY),
    ]
    assert script.errors == []
    assert not [w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow)]
    # The one who rode last is offered first.
    picked = []

    def pick(picker):
        picked.append(picker.chosen().name)
        return picker.DialogCode.Accepted

    monkeypatch.setattr(RiderPicker, "exec", pick)
    script.steps.append(lambda w: w.close())
    assert app.run_riders(qapp, riders, fake=True) == 0
    assert picked == ["Bob"] and script.seen[-1][0] == "Bob"


def test_closing_the_real_picker_does_not_end_the_next_window(data, qapp, monkeypatch):
    riders = Riders(data / "t.db")
    script = Script(monkeypatch, add("Bob"), lambda w: w.close(), lambda w: w.close())
    app.run_riders(qapp, riders, fake=True)
    QTimer.singleShot(200, lambda: qapp.activeModalWidget().accept())
    app.run_riders(qapp, riders, fake=True)
    assert len(script.seen) == 3 and script.steps == []  # the step ran: it was not quit early


def test_cancelled_picker_opens_nothing(data, qapp, monkeypatch):
    riders = Riders(data / "t.db")
    script = Script(monkeypatch, add("Bob"), lambda w: w.close())
    app.run_riders(qapp, riders, fake=True)
    monkeypatch.setattr(RiderPicker, "exec", lambda p: p.DialogCode.Rejected)
    assert app.run_riders(qapp, riders, fake=True) == 0 and len(script.seen) == 2


def test_unusable_database_is_reported(data, qapp, monkeypatch):
    script = Script(monkeypatch)
    riders = Riders(data / "t.db")
    riders.root.write_bytes(b"no database" * 500)
    assert app.run_riders(qapp, riders, fake=True) == 1
    riders.root.unlink()
    riders.library.write_bytes(b"no database" * 500)
    assert app.run_riders(qapp, riders, fake=True) == 1
    assert len(script.errors) == 2 and script.seen == []


def test_no_second_rider_while_the_trainer_is_busy(data, qapp, monkeypatch):
    script = Script(monkeypatch, add("Bob"))
    real = app.TrainerBridge.shutdown
    monkeypatch.setattr(app.TrainerBridge, "shutdown", lambda self: real(self) and False)
    assert app.run_riders(qapp, Riders(data / "t.db"), fake=True) == 1
    assert "trainer" in script.errors[0] and len(script.seen) == 1


def test_other_riders_data_stays_apart(data, qapp, monkeypatch):
    riders = Riders(data / "t.db")

    def rename(window):
        window._repo.set_setting(NAME, "Bobby")
        window._repo.set_setting("ftp_w", "123")
        window.profile.profile_changed.emit()
        window.close()

    Script(monkeypatch, add("Bob"), rename)
    app.run_riders(qapp, riders, fake=True)
    assert [r.name for r in riders.list()] == ["Bobby", "Rider 1"]
    for rider, ftp in zip(riders.list(), ("123", "200"), strict=True):
        conn = riders.open(rider.path)
        assert Repo(conn).get_setting("ftp_w") == ftp
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("SELECT * FROM main.workouts")
        conn.close()


def test_simulated_devices_belong_together(qtbot):
    bridge = app.make_bridge(fake=True)
    try:
        readings = []
        bridge.pm_reading.connect(readings.append)
        bridge.connect_trainer("t")
        bridge.start()
        bridge.set_target_power(200)
        bridge.pm_connect("m")
        qtbot.waitUntil(lambda: bool(readings) and readings[-1].watts > 50, timeout=10_000)
    finally:
        assert bridge.shutdown()


def test_real_devices_share_the_radio(monkeypatch):
    made = {}
    monkeypatch.setattr(app, "TrainerBridge", lambda trainer, **kw: made.update(t=trainer, **kw))
    app.make_bridge(fake=False)
    devices = [made[key]() for key in ("t", "hr_factory", "pm_factory")]
    assert len({id(d._radio) for d in devices}) == 1
