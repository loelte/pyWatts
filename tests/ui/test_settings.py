import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from pywatts.storage.db import open_db
from pywatts.storage.repo import TRAINER_ADDR, TRAINER_NAME, UNITS, Repo
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
    assert page.trainer.text() == "None" and not page.forget_button.isEnabled()
    page.scan_button.click()
    assert bridge.scans == 1 and not page.scan_button.isEnabled()
    bridge.scan_finished.emit(DEVICES)
    assert page.scan_button.isEnabled() and page.devices.count() == 2
    with qtbot.waitSignal(page.settings_changed):
        page.select_button.click()
    assert repo.get_setting(TRAINER_ADDR) == "BB"
    assert "strong" in page.trainer.text() and "BB" in page.trainer.text()
    with qtbot.waitSignal(page.settings_changed):
        page.forget_button.click()
    assert repo.get_setting(TRAINER_ADDR) == "" == repo.get_setting(TRAINER_NAME)
    assert page.trainer.text() == "None"


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
    assert "CC" in page.trainer.text()


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
