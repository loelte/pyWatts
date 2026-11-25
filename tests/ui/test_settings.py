import pytest
from PyQt6.QtCore import QObject, QPoint, QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication

from pywatts.storage.db import open_db
from pywatts.storage.repo import (
    HR_ADDR,
    HR_NAME,
    PM_ADDR,
    PM_NAME,
    POWER_MATCH,
    TRAINER_ADDR,
    TRAINER_NAME,
    UNITS,
    Repo,
)
from pywatts.trainer.base import DeviceInfo
from pywatts.ui.settings import SettingsPage


class FakeBridge(QObject):
    scan_finished = pyqtSignal(list)
    error = pyqtSignal(str)
    scans = 0

    def scan(self, timeout=5.0):
        self.scans += 1


@pytest.fixture
def repo(tmp_path):
    conn = open_db(tmp_path / "t.db")
    yield Repo(conn)
    conn.close()


@pytest.fixture
def bridge(qapp):
    return FakeBridge()


@pytest.fixture
def page(qtbot, repo, bridge):
    p = SettingsPage(repo, bridge)
    qtbot.addWidget(p)
    return p


DEVICES = [DeviceInfo("weak", "AA", -90), DeviceInfo("strong", "BB", -40)]


def test_units(page, repo, qtbot):
    with qtbot.waitSignal(page.settings_changed):
        page.units.setCurrentIndex(1)
    assert repo.get_setting(UNITS) == "imperial"


def test_scan_select_forget(page, repo, bridge, qtbot):
    assert (
        page.default_labels[TRAINER_ADDR].text() == "None"
        and not page.forget_buttons[TRAINER_ADDR].isEnabled()
    )
    page.scan_button.click()
    assert bridge.scans == 1 and not page.scan_button.isEnabled()
    bridge.scan_finished.emit(DEVICES)
    assert page.scan_button.isEnabled() and page.devices.count() == 2
    with qtbot.waitSignal(page.settings_changed):
        page.select_button.click()
    assert repo.get_setting(TRAINER_ADDR) == "BB"
    assert (
        "strong" in page.default_labels[TRAINER_ADDR].text()
        and "BB" in page.default_labels[TRAINER_ADDR].text()
    )
    with qtbot.waitSignal(page.settings_changed):
        page.forget_buttons[TRAINER_ADDR].click()
    assert repo.get_setting(TRAINER_ADDR) == "" == repo.get_setting(TRAINER_NAME)
    assert page.default_labels[TRAINER_ADDR].text() == "None"


def test_unrequested_results_and_errors_ignored(page, bridge):
    bridge.scan_finished.emit(DEVICES)
    bridge.error.emit("other")
    assert page.devices.count() == 0 and page.message.text() == ""


def test_scan_error_then_late_result(page, bridge):
    page.scan_button.click()
    bridge.error.emit("no adapter")
    assert "no adapter" in page.message.text() and page.scan_button.isEnabled()
    bridge.scan_finished.emit([])
    assert "no adapter" in page.message.text()


def test_scan_raises_or_finds_nothing_or_hangs(page, bridge, monkeypatch):
    page.scan_button.click()
    bridge.scan_finished.emit([])
    assert "No trainer" in page.message.text() and not page.select_button.isEnabled()
    page.scan_button.click()
    page._scan_guard.timeout.emit()
    assert page.scan_button.isEnabled()

    def boom(_t):
        raise RuntimeError("shut down")

    monkeypatch.setattr(bridge, "scan", boom)
    page.scan_button.click()
    assert "shut down" in page.message.text() and page.scan_button.isEnabled()


def test_store_failure_is_shown(page, repo, monkeypatch):
    def boom(*_):
        raise OSError("locked")

    monkeypatch.setattr(type(repo), "set_setting", boom)
    page.units.setCurrentIndex(1)
    assert "locked" in page.message.text() and page.units.currentIndex() == 0


def test_show_rereads_trainer(page, repo):
    repo.set_setting(TRAINER_ADDR, "CC")
    page.show()
    assert "CC" in page.default_labels[TRAINER_ADDR].text()


def test_ride_layout_add_remove_arrange(page, repo):
    from pywatts.storage.repo import RIDE_TILES
    from pywatts.ui.ride_layout import TILES

    assert page.shown_list.count() == len(TILES) and page.available_list.count() == 0
    page.shown_list.setCurrentRow(0)
    page.remove_tile_button.click()  # power
    assert repo.get_setting(RIDE_TILES).split(",")[0] == "target"
    assert page.available_list.item(0).text() == "Power"
    page.shown_list.setCurrentRow(0)
    page.down_tile_button.click()
    assert repo.get_setting(RIDE_TILES).startswith("cadence,target,")
    assert page.shown_list.currentRow() == 1
    page.available_list.setCurrentRow(0)
    page.add_tile_button.click()
    assert repo.get_setting(RIDE_TILES).endswith(",power")
    page.reset_tiles_button.click()
    assert repo.get_setting(RIDE_TILES) == ""


def test_nav_setting_emits(page, repo):
    from pywatts.storage.repo import NAV_HIDDEN

    page.nav_visible.setChecked(False)
    assert repo.get_setting(NAV_HIDDEN) == "1"
    page.nav_visible.setChecked(True)
    assert repo.get_setting(NAV_HIDDEN) == ""


def test_default_devices_are_listed_and_forgotten_separately(page, repo):
    for addr_key, name_key, name in ((TRAINER_ADDR, TRAINER_NAME, "T"), (HR_ADDR, HR_NAME, "H")):
        repo.set_setting(addr_key, f"addr-{name}")
        repo.set_setting(name_key, name)
    page.refresh()
    assert page.default_labels[TRAINER_ADDR].text() == "T (addr-T)"
    assert page.default_labels[HR_ADDR].text() == "H (addr-H)"
    page.forget_buttons[HR_ADDR].click()
    assert repo.get_setting(HR_ADDR) == "" == repo.get_setting(HR_NAME)
    assert page.default_labels[HR_ADDR].text() == "None"
    assert not page.forget_buttons[HR_ADDR].isEnabled()
    assert repo.get_setting(TRAINER_ADDR) == "addr-T"


def _wheel(widget):
    """True if the widget consumed it. Qt hands only real events on to the parents."""
    pos = QPointF(widget.rect().center())
    event = QWheelEvent(
        pos,
        QPointF(widget.mapToGlobal(pos.toPoint())),
        QPoint(),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    event.accept()
    QApplication.sendEvent(widget, event)
    return event.isAccepted()


@pytest.fixture
def small(page, qtbot):
    page.resize(800, 400)
    page.show()
    qtbot.waitExposed(page)
    qtbot.waitUntil(lambda: page.scroll_area.verticalScrollBar().maximum() > 0)
    return page


def test_wheel_is_left_to_the_page(small, repo):
    bar = small.scroll_area.verticalScrollBar()
    for widget in (small.units, small.devices.viewport(), small.shown_list.viewport()):
        assert not _wheel(widget)
    assert _wheel(small.scroll_area.viewport()) and bar.value() > 0
    assert small.units.currentIndex() == 0 and repo.get_setting(UNITS) == "metric"


def test_lists_show_all_rows(small, bridge, qtbot):
    small.scan_button.click()
    bridge.scan_finished.emit([DeviceInfo(f"t{i}", f"A{i}", -i) for i in range(12)])

    def fits():
        for widget in (small.devices, small.shown_list):
            last = widget.visualItemRect(widget.item(widget.count() - 1))
            assert last.bottom() <= widget.viewport().height()

    qtbot.waitUntil(fits)


def test_scan_does_not_move_the_page(small, qtbot):
    small.activateWindow()
    small.scan_button.setFocus()
    qtbot.waitUntil(small.scan_button.hasFocus)
    bar = small.scroll_area.verticalScrollBar()
    before = bar.value()
    small.scan_button.click()
    assert bar.value() == before and small.devices.hasFocus()


def test_message_is_outside_the_scroll_area(small):
    assert small.message.isHidden()
    small.scan_button.click()
    assert small.message.isVisible() and not small.scroll_area.isAncestorOf(small.message)


def test_power_meter_settings(page, repo, qtbot):
    repo.set_setting(PM_ADDR, "AA")
    repo.set_setting(PM_NAME, "Assioma")
    page.refresh()
    assert page.default_labels[PM_ADDR].text() == "Assioma (AA)"
    page.forget_buttons[PM_ADDR].click()
    assert repo.get_setting(PM_ADDR) == "" and not page.forget_buttons[PM_ADDR].isEnabled()
    assert not page.power_match.isChecked()
    with qtbot.waitSignal(page.settings_changed):
        page.power_match.click()
    assert repo.get_setting(POWER_MATCH) == "1"
    page.refresh()
    assert page.power_match.isChecked()
    page.power_match.click()
    assert repo.get_setting(POWER_MATCH) == ""
