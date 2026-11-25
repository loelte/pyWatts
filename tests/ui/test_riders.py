from contextlib import closing

import pytest

from pywatts.storage.repo import NAME, Repo
from pywatts.storage.riders import Riders
from pywatts.trainer.fake import FakeTrainer
from pywatts.trainer.thread import TrainerBridge
from pywatts.ui.main_window import MainWindow
from pywatts.ui.ride_controller import RideController
from pywatts.ui.riders import RiderButton, RiderPicker


class Button(RiderButton):
    name: str | None = "Bob"
    sure = True

    def ask_name(self):
        return self.name

    def confirm_remove(self, rider):
        return self.sure

    def show_info(self, text):
        self.infos.append(text)

    def show_error(self, text):
        self.errors.append(text)


@pytest.fixture
def riders(tmp_path):
    return Riders(tmp_path / "t.db")


@pytest.fixture
def repo(riders):
    with closing(riders.open(riders.root)) as conn:
        Repo(conn).set_setting(NAME, "Ann")
        yield Repo(conn)


@pytest.fixture
def button(qtbot, riders, repo):
    forgotten = []
    current = riders.register(repo.connection, riders.root)
    b = Button(riders, repo, current, forgotten.append)
    b.forgotten, b.infos, b.errors, b.asked = forgotten, [], [], []
    b.switch_requested.connect(lambda r, new: b.asked.append((r.name, new)))
    qtbot.addWidget(b)
    return b


def entries(button, menu=None):
    button.fill_menu() if menu is None else None
    return {a.text(): a for a in (menu or button.menu()).actions() if a.text()}


def test_picker_preselects_the_last(qtbot, riders, repo):
    riders.register(repo.connection, riders.root)
    bob = riders.add("Aaron", repo)
    picker = RiderPicker(riders.list())
    qtbot.addWidget(picker)
    assert picker.chosen().name == "Ann"
    picker.names.setCurrentRow(0)
    assert picker.chosen().id == bob.id


def test_add_and_switch(button, riders):
    assert list(entries(button)) == ["Ann", "Add rider…"]
    entries(button)["Add rider…"].trigger()
    assert button.asked == [("Bob", True)]
    found = entries(button)
    assert list(found) == ["Ann", "Bob", "Add rider…", "Remove rider"]
    assert found["Ann"].isChecked() and not found["Bob"].isChecked()
    found["Bob"].trigger()
    entries(button)["Ann"].trigger()  # already riding
    assert button.asked == [("Bob", True), ("Bob", False)] and button.errors == []


def test_add_cancelled_or_refused(button, riders):
    button.name = None
    entries(button)["Add rider…"].trigger()
    button.name = "ann"
    entries(button)["Add rider…"].trigger()
    assert button.asked == [] and "already" in button.errors[0] and len(riders.list()) == 1


def test_remove(button, riders, repo):
    bob = riders.add("B&B", repo)

    def remove():
        entries(button, entries(button)["Remove rider"].menu())["B&&B…"].trigger()

    button.sure = False
    remove()
    assert button.forgotten == [] and len(riders.list()) == 2
    button.sure = True
    remove()
    assert [r.id for r in button.forgotten] == [bob.id]
    assert len(riders.list()) == 1 and "removed" in button.infos[0]


def test_login_that_cannot_be_forgotten_stops_the_removal(button, riders, repo):
    riders.add("Bob", repo)

    def fail(_r):
        raise RuntimeError("no keychain")

    button._forget_login = fail
    entries(button, entries(button)["Remove rider"].menu())["Bob…"].trigger()
    assert button.errors == ["no keychain"] and len(riders.list()) == 2


@pytest.fixture
def window(qtbot, button, repo):
    bridge = TrainerBridge(FakeTrainer)
    controller = RideController(repo, bridge)
    window = MainWindow(repo, bridge, controller, riders=button)
    qtbot.addWidget(window)
    window.show()
    yield window
    controller.shutdown()
    bridge.shutdown()


def test_window_names_the_rider_if_there_are_several(window, button):
    assert window.windowTitle() == "pyWatts"
    window.editor.confirm_discard = lambda: False
    entries(button)["Add rider…"].trigger()
    assert window.isVisible() and window.next_rider is None  # unsaved edits kept
    assert window.windowTitle() == "pyWatts – Ann"
    window._repo.set_setting(NAME, "Anna")
    window.profile.profile_changed.emit()
    assert window.windowTitle() == "pyWatts – Anna" and button.text() == "Anna"


def test_switch_closes_the_window(window, button, riders, repo):
    bob = riders.add("Bob", repo)
    entries(button)["Bob"].trigger()
    assert not window.isVisible() and window.next_rider == bob and not window.next_is_new


def test_no_switch_during_a_ride(window, button, riders, repo, monkeypatch):
    bob = riders.add("Bob", repo)
    with monkeypatch.context() as m:
        m.setattr(RideController, "is_active", property(lambda _s: True))
        assert list(entries(button)) == ["Not during a ride"]
        window.switch_rider(bob)
        assert window.isVisible() and window.next_rider is None


@pytest.fixture
def styled(qapp):
    qapp.setStyleSheet("QListWidget::item { padding: 30px; }")
    yield
    qapp.setStyleSheet("")


def test_rows_do_not_overlap(styled, window, qtbot, riders, repo):
    """The stylesheet counts although a row is selected before the list is shown."""
    riders.add("Bob", repo)
    picker = RiderPicker(riders.list())
    qtbot.addWidget(picker)
    picker.show()
    for rows in (window.sidebar, picker.names):
        first, second = (rows.visualItemRect(rows.item(i)) for i in range(2))
        assert first.height() > 60 and second.top() > first.bottom()
