import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QLabel, QPushButton

from pywatts import platform
from pywatts.ai import launch
from pywatts.ai.pty import PtyBridge
from pywatts.core.plan import PlanUnit
from pywatts.storage.db import open_db
from pywatts.storage.plan_repo import PlanRepo
from pywatts.storage.repo import COACH_COMMAND, COACH_CONSENT, COACH_PATH, Repo
from pywatts.ui import coach_panel
from pywatts.ui.coach_panel import CoachPanel
from pywatts.ui.widgets.terminal import line_text

DAY = date(2026, 3, 4)


@dataclass
class Launch:
    env: dict
    mcp_config: Path
    prompt: str


class FakeCoach(QObject):
    plan_changed = pyqtSignal()
    workouts_changed = pyqtSignal()
    activity = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.error = None

    def prepare(self):
        self.calls.append("prepare")
        if self.error:
            raise self.error
        return Launch({"PYW_TOKEN": "t"}, Path("mcp.json"), "be a coach")

    def stop(self):
        self.calls.append("stop")


class FakeBridge(QObject):
    ready_read = pyqtSignal()
    failed = pyqtSignal(str)
    finished = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.starts, self.is_running = [], False

    def start(self, argv, cwd, env, rows, cols):
        self.starts.append((argv, cwd, env, rows, cols))
        self.is_running = True

    def stop(self):
        self.is_running = False

    def read(self, _n=0):
        return ""

    def write(self, text):
        pass

    def resize(self, rows, cols):
        pass


class Panel(CoachPanel):
    answer = True
    asked = 0

    def ask_consent(self):
        self.asked += 1
        return self.answer


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "data_dir", lambda: tmp_path)
    conn = open_db(tmp_path / "t.db")
    yield conn
    conn.close()


@pytest.fixture
def repo(conn):
    repo = Repo(conn)
    repo.set_setting(COACH_PATH, sys.executable)
    return repo


@pytest.fixture
def plans(conn):
    return PlanRepo(conn)


@pytest.fixture
def coach(qtbot):
    return FakeCoach()


@pytest.fixture
def make(qtbot, repo, plans, coach):
    def build(factory=FakeBridge):
        panel = Panel(repo, plans, coach, bridge_factory=factory)
        qtbot.addWidget(panel, before_close_func=lambda p: p.shutdown())
        panel.resize(320, 500)
        panel.show()
        qtbot.waitExposed(panel)
        return panel

    return build


def test_nothing_starts_by_itself(make, coach):
    panel = make()
    assert not panel.is_running and panel._bridge.starts == [] and coach.calls == []
    assert panel.status.text() == "Stopped"
    assert panel.placeholder.isVisible() and "claude.com" in panel.placeholder.text()
    assert not panel.restart_button.isEnabled()
    assert panel.asked == 0


def test_no_consent_no_start(make, coach, repo):
    panel = make()
    panel.answer = False
    panel.start_button.click()
    assert panel.asked == 1 and not panel.is_running
    assert panel._bridge.starts == [] and "prepare" not in coach.calls
    assert repo.get_setting(COACH_CONSENT) == ""
    panel.start_button.click()
    assert panel.asked == 2


def test_consent_text_names_the_data(make, monkeypatch):
    for word in ("rides", "heart-rate", "name", "age", "weight", "provider", "undone"):
        assert word in coach_panel.CONSENT


def test_start_stop_restart(make, coach, repo, tmp_path):
    panel = make()
    panel.start_button.click()
    assert panel.is_running and panel.status.text() == "Running"
    assert repo.get_setting(COACH_CONSENT) == "1"
    argv, cwd, env, rows, cols = panel._bridge.starts[0]
    assert argv[0] == str(Path(sys.executable).absolute())
    assert "--restricted" in argv and argv[-1] == "be a coach"
    assert cwd == tmp_path / "coach" / repo.rider_id and cwd.is_dir()
    assert env == {"PYW_TOKEN": "t"}
    assert (rows, cols) == panel.terminal.grid() and 20 < cols < 60
    assert panel.terminal.isVisible()
    panel.restart_button.click()
    assert panel.is_running and len(panel._bridge.starts) == 2 and panel.asked == 1
    coach.calls.clear()
    panel.start_button.click()
    assert not panel.is_running and not panel._bridge.is_running and coach.calls == ["stop"]
    assert panel.status.text() == "Stopped" and panel.placeholder.isVisible()


def test_own_command(make, repo):
    repo.set_setting(COACH_COMMAND, f'"{sys.executable}" --config {{mcp_config}}')
    panel = make()
    panel.start()
    assert panel._bridge.starts[0][0][1:] == ["--config", "mcp.json"]


def test_end_of_the_program(make, coach):
    panel = make()
    panel.start()
    coach.calls.clear()
    panel._bridge.finished.emit(3)
    assert panel.status.text() == "Exited 3" and not panel.is_running
    assert panel.terminal.isVisible()  # its last message stays readable
    assert coach.calls == ["stop"]
    panel.start_button.click()
    assert panel.is_running


def test_start_errors_are_shown(make, coach, repo, monkeypatch):
    monkeypatch.setattr(launch.shutil, "which", lambda _n: None)
    panel = make()
    panel.start_button.click()
    assert not panel.is_running and "not found" in panel.message.text()
    assert panel.message.isVisible() and panel.placeholder.isVisible()
    assert coach.calls[-1] == "stop"
    monkeypatch.undo()
    coach.error = OSError("pipe in use")
    panel.start_button.click()
    assert "pipe in use" in panel.message.text()
    coach.error = None
    panel._bridge.start = lambda *a: (_ for _ in ()).throw(OSError("denied"))
    panel.start_button.click()
    assert "denied" in panel.message.text() and not panel.is_running
    del panel._bridge.start


def test_failed_start(make, coach):
    panel = make()
    panel.start()
    panel._bridge.failed.emit("no such file")
    panel._bridge.finished.emit(None)
    assert panel.status.text() == "Stopped" and "no such file" in panel.message.text()
    panel.start()
    assert not panel.message.isVisible()


def rows(panel):
    widget = panel.changes.widget()
    return [
        (label.text(), button.text(), button.isEnabled())
        for frame in widget.children()
        if (button := frame.findChild(QPushButton)) and (label := frame.findChild(QLabel))
    ]


def undo_button(panel, index):
    return [b for b in panel.changes.widget().findChildren(QPushButton)][index]


def test_changes_and_undo(make, plans, coach, qtbot):
    unit = PlanUnit(id="u", day=DAY, kind="rest", title="Rest")
    plans.apply("user", "mine", [unit])
    plans.apply("ai", "Added <b>two</b> units", [PlanUnit(id="a", day=DAY, kind="rest", title="A")])
    panel = make()
    panel.changes_button.click()
    assert rows(panel) == [("Added <b>two</b> units", "Undo", True)]
    plans.apply("ai", "Later", [PlanUnit(id="b", day=DAY, kind="rest", title="B")])
    coach.plan_changed.emit()
    assert [r[0] for r in rows(panel)] == ["Later", "Added <b>two</b> units"]
    with qtbot.waitSignal(panel.undone):
        undo_button(panel, 1).click()
    assert plans.get_unit("a") is None and plans.get_unit("b") is not None
    assert rows(panel)[1][1:] == ("Undone", False)
    panel.changes_button.click()
    assert not panel.changes.isVisible()


def test_undo_conflict_is_shown(make, plans, qtbot):
    plans.apply("ai", "Add", [PlanUnit(id="a", day=DAY, kind="rest", title="A")])
    plans.apply("user", "Edit", [PlanUnit(id="a", day=DAY, kind="note", title="Mine")])
    panel = make()
    panel.changes_button.click()
    with qtbot.assertNotEmitted(panel.undone):
        undo_button(panel, 0).click()
    assert "changed again" in panel.message.text()
    assert plans.get_unit("a").title == "Mine"


def test_storage_errors_do_not_escape(make, conn, coach):
    panel = make()
    conn.close()
    panel.changes_button.click()
    assert "Error" in panel.message.text()
    coach.workouts_changed.emit()
    panel.start_button.click()
    assert not panel.is_running


def test_activity(make, coach):
    panel = make()
    coach.activity.emit("add_units " + "x" * 200)
    assert panel.activity.text().startswith("add_units")
    assert panel.minimumSizeHint().width() <= 320


def test_shutdown_is_idempotent(make, coach):
    panel = make()
    panel.start()
    panel.shutdown()
    panel.shutdown()
    assert not panel.is_running and not panel._bridge.is_running


def test_real_program(make, repo, qtbot, tmp_path):
    script = tmp_path / "child.py"
    script.write_text(
        "import os, sys\nprint('\\033[32mtoken', os.environ['PYW_TOKEN'], sys.argv[1], "
        "flush=True)\nprint('you said', input())\nsys.exit(4)\n"
    )
    repo.set_setting(COACH_COMMAND, f'"{sys.executable}" "{script}" {{prompt}}')
    panel = make(PtyBridge)
    panel.start()

    def shown():
        emulator = panel.terminal.emulator
        return "\n".join(line_text(emulator.line(i)) for i in range(emulator.screen.lines))

    qtbot.waitUntil(lambda: "token t be a coach" in shown(), timeout=10_000)
    qtbot.keyClicks(panel.terminal, "hi\r")
    qtbot.waitUntil(lambda: "you said hi" in shown(), timeout=10_000)
    qtbot.waitUntil(lambda: panel.status.text() == "Exited 4", timeout=10_000)
