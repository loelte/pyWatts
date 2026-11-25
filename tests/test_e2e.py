"""Whole app with FakeTrainer: create workout -> ride -> history -> FIT export."""

import subprocess
import sys

import pytest
from fit_tool.fit_file import FitFile

from pywatts.core.workout import Ramp, Steady, Workout
from pywatts.storage.db import open_db
from pywatts.storage.fit_export import export_fit
from pywatts.storage.repo import Repo
from pywatts.trainer.base import ConnectionState
from pywatts.trainer.fake import FakeTrainer
from pywatts.trainer.thread import TrainerBridge
from pywatts.ui.main_window import RIDE, MainWindow
from pywatts.ui.ride_controller import RideController


@pytest.fixture
def env(tmp_path, qtbot):
    conn = open_db(tmp_path / "e2e.db")
    repo = Repo(conn)
    bridge = TrainerBridge(FakeTrainer)
    controller = RideController(repo, bridge)
    yield repo, bridge, controller
    controller.shutdown()
    bridge.shutdown()
    conn.close()


def connect(qtbot, bridge):
    with qtbot.waitSignal(bridge.scan_finished, timeout=8000) as scan:
        bridge.scan()
    bridge.connect_trainer(scan.args[0][0].addr)
    qtbot.waitUntil(lambda: bridge.state is ConnectionState.CONNECTED, timeout=8000)


def test_ride_to_fit(env, qtbot, tmp_path):
    repo, bridge, controller = env
    repo.save_workout(Workout("E2E", [Steady(2, 0.5), Ramp(2, 0.5, 1.0)]))
    window = MainWindow(repo, bridge, controller)
    qtbot.addWidget(window)
    window.editor.refresh()
    window.editor.ride_requested.emit(repo.list_workouts()[0])
    assert window.stack.currentIndex() == RIDE
    connect(qtbot, bridge)
    with qtbot.waitSignal(controller.session_finished, timeout=15000) as done:
        controller.start()
    session = repo.get_session(done.args[0])
    assert session.status == "completed"
    samples = repo.get_samples(session.id)
    assert len(samples) >= 3
    assert any(s.power for s in samples)
    out = tmp_path / "ride.fit"
    export_fit(session, samples, out)
    assert len(list(FitFile.from_file(str(out)).records)) > len(samples)


def test_abort_on_close(env, qtbot):
    repo, bridge, controller = env
    window = MainWindow(repo, bridge, controller)
    qtbot.addWidget(window)
    window.ride.set_workout(Workout("Long", [Steady(600, 0.5)]))
    connect(qtbot, bridge)
    controller.start()
    qtbot.wait(1500)
    window.confirm_abort = lambda: False
    assert not window.close()
    window.confirm_abort = lambda: True
    assert window.close()
    assert repo.list_sessions()[0].status == "aborted"


def test_core_dependency_rule():
    code = (
        "import sys;from pywatts.core import metrics, runner, workout, zones, zwo;"
        "bad=[m for m in ('PyQt6','bleak','sqlite3') if m in sys.modules];"
        "sys.exit(str(bad) if bad else 0)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_window_fits_and_repeats_its_icon(tmp_path, qtbot, monkeypatch):
    """The taskbar stays without icon if the window was busy when it was asked for it."""
    conn = open_db(tmp_path / "t.db")
    repo = Repo(conn)
    bridge = TrainerBridge(FakeTrainer)
    controller = RideController(repo, bridge)
    window = MainWindow(repo, bridge, controller)
    qtbot.addWidget(window)
    icons = []
    monkeypatch.setattr(window, "setWindowIcon", icons.append)
    with qtbot.waitExposed(window):
        window.show()
    qtbot.waitUntil(lambda: len(icons) == 1)
    available = window.screen().availableGeometry()
    if window.minimumSizeHint().width() <= available.width():
        assert available.contains(window.frameGeometry())
    window.hide()
    window.setGeometry(50, 60, 1230, 690)
    window.show()  # placed only once: the user's size stays
    qtbot.wait(20)
    assert window.height() == 690 and len(icons) == 1
    bridge.shutdown()
    conn.close()
