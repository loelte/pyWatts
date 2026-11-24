"""Training plan in the main window: rides, imports, library, AI changes, settings."""

import json
from datetime import date, datetime, timedelta

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from pywatts.ai.service import CoachService
from pywatts.core.plan import PlanUnit
from pywatts.core.workout import Steady, Workout
from pywatts.storage.db import open_db
from pywatts.storage.plan_repo import PlanRepo
from pywatts.storage.repo import COACH_COMMAND, COACH_PATH, ImportedRide, Repo, Sample
from pywatts.trainer.fake import FakeTrainer
from pywatts.trainer.thread import TrainerBridge
from pywatts.ui.main_window import LIBRARY, PLAN, RIDE, MainWindow
from pywatts.ui.ride_controller import RideController

W = Workout("Planned", [Steady(600, 0.8)])


class Sync(QObject):
    """Only what MainWindow and its pages connect to."""

    import_finished = pyqtSignal(object)
    import_progress = pyqtSignal(object)
    uploads_changed = pyqtSignal(int)
    connected_changed = pyqtSignal(bool)
    error = pyqtSignal(str)
    connected = auto_upload = importing = False


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "t.db")
    yield conn
    conn.close()


@pytest.fixture
def repo(conn):
    return Repo(conn)


@pytest.fixture
def plans(conn):
    return PlanRepo(conn)


@pytest.fixture
def window(qtbot, repo, plans, tmp_path):
    bridge = TrainerBridge(FakeTrainer)
    controller = RideController(repo, bridge)
    coach = CoachService(repo, plans, controller, tmp_path / "coach")
    window = MainWindow(repo, bridge, controller, None, coach)
    window.bridge, window.coach = bridge, coach
    qtbot.addWidget(window)
    window.show()
    window.sidebar.setCurrentRow(PLAN)
    yield window
    window.coach_panel.shutdown()
    coach.stop()
    controller.shutdown()
    bridge.shutdown()


def unit(**kw):
    return PlanUnit(**{"id": "u", "day": date.today(), "kind": "indoor", "title": "T"} | kw)


def imported(repo):
    start = datetime.now().astimezone().replace(hour=0, minute=1)
    samples = [Sample(float(t), 150, None, None, 0, None) for t in range(600)]
    ride = ImportedRide(start, start + timedelta(seconds=600), "R", samples)
    return repo.import_session(ride, "file", "1", "1").session_id


def test_pages_match_the_sidebar(window):
    assert window.stack.currentWidget() is window.plan
    assert window.sidebar.item(PLAN).text() == "Training Plan"


def test_planned_ride(qtbot, window, repo, plans):
    plans.apply("user", "x", [unit(workout=W)])
    window.plan.ride_requested.emit(W, "u")
    assert window.sidebar.currentRow() == RIDE
    with qtbot.waitSignal(window.bridge.connection_changed, timeout=5000):
        window.bridge.connect_trainer("fake")
    qtbot.waitUntil(lambda: window.controller._connected(), timeout=5000)
    window.controller.start()
    window.controller.stop()
    link = plans.links()["u"]
    assert link.how == "started" and repo.get_session(link.session_id).status == "aborted"


def test_ride_from_the_library_is_not_linked(window, plans):
    plans.apply("user", "x", [unit(workout=W)])
    window.plan.ride_requested.emit(W, "u")
    window.editor.ride_requested.emit(W)
    assert window.controller._unit_id is None


def test_finished_rides_are_matched(window, repo, plans):
    plans.apply("user", "x", [unit(target_s=600)])
    sid = imported(repo)
    window.controller.session_finished.emit(sid)
    assert plans.links()["u"].session_id == sid


def test_imports_are_matched(qtbot, repo, plans):
    bridge = TrainerBridge(FakeTrainer)
    controller = RideController(repo, bridge)
    sync = Sync()
    window = MainWindow(repo, bridge, controller, sync)
    qtbot.addWidget(window)
    plans.apply("user", "x", [unit(target_s=600)])
    imported(repo)
    sync.import_finished.disconnect(window.sync._on_import_finished)
    sync.import_finished.emit(None)
    assert "u" in plans.links()
    controller.shutdown()
    bridge.shutdown()


def test_matching_failure_is_no_crash(window, monkeypatch):
    monkeypatch.setattr(window._plans, "auto_match", lambda *_: 1 / 0)
    window.controller.session_finished.emit(1)


def test_open_in_library(window, repo):
    repo.save_workout(W)
    window.editor.refresh()
    window.plan.workout_requested.emit(W.id)
    assert window.sidebar.currentRow() == LIBRARY and window.editor.model.id == W.id


def test_unsaved_edits_keep_the_library_closed(window, repo):
    repo.save_workout(W)
    window.editor.refresh()
    window.editor.name_edit.setText("x")
    window.editor.name_edit.textEdited.emit("x")
    window.editor.ask_unsaved = lambda: "cancel"
    window.plan.workout_requested.emit(W.id)
    assert window.sidebar.currentRow() == PLAN and window.editor.model.id != W.id


def test_ai_changes_are_shown_and_undone(window, plans):
    day = date.today().isoformat()
    segments = [{"type": "steady", "duration_s": 600, "power": 0.8}]
    units = [{"day": day, "kind": "indoor", "title": "AI ride", "segments": segments}]
    call = {"name": "add_units", "arguments": {"units": units}}
    change = json.loads(window.coach._handle("call", call))["change_id"]
    (stored,) = plans.units_between(date.today(), date.today())
    assert window.plan._data.find(("unit", stored.id)) is not None
    assert window.editor.library.count() == 1
    window.coach_panel._undo(change)
    assert window.plan._data.find(("unit", stored.id)) is None
    assert window.editor.library.count() == 0


def test_coach_settings(window, repo):
    page = window.settings
    page.coach_path.setText(" C:/x/claude.exe ")
    page.coach_path.editingFinished.emit()
    page.coach_command.setText("other --mcp {mcp_config}")
    page.coach_command.editingFinished.emit()
    assert repo.get_setting(COACH_PATH) == "C:/x/claude.exe"
    assert repo.get_setting(COACH_COMMAND) == "other --mcp {mcp_config}"


def test_closing_ends_the_coach(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window.coach_panel, "shutdown", lambda: calls.append(1))
    window.close()
    assert calls
    monkeypatch.undo()
