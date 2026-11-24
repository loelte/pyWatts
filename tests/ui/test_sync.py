from datetime import UTC, datetime, timedelta

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from pywatts.core.workout import Workout
from pywatts.storage.db import open_db
from pywatts.storage.fit_export import fit_bytes
from pywatts.storage.repo import ImportedRide, Repo, Sample, Upload
from pywatts.sync.base import SyncError
from pywatts.sync.service import ImportSummary, SyncService
from pywatts.sync.strava import UploadStatus
from pywatts.sync.thread import SyncBridge
from pywatts.trainer.fake import FakeTrainer
from pywatts.trainer.thread import TrainerBridge
from pywatts.ui.history import HistoryPage
from pywatts.ui.main_window import SYNC, MainWindow
from pywatts.ui.ride_controller import RideController
from pywatts.ui.sync import SyncPage


class FakeRides(QObject):
    session_finished = pyqtSignal(int)
    state_changed = pyqtSignal(object)
    is_active = False


class FakeStrava:
    connected = False
    answer = UploadStatus("5", "77")

    def connect(self, client_id, secret, cancel):
        if secret == "wait":
            cancel.wait(5)
            raise SyncError("Login cancelled.")
        self.connected = True

    def disconnect(self):
        self.connected = False

    def upload(self, fit, name, external_id):
        return self.answer


@pytest.fixture
def repo(tmp_path):
    conn = open_db(tmp_path / "t.db")
    yield Repo(conn)
    conn.close()


@pytest.fixture
def strava():
    return FakeStrava()


@pytest.fixture
def service(repo, strava, qtbot):
    rides = FakeRides()
    bridge = SyncBridge(lambda: strava)
    with qtbot.waitSignal(bridge.connected_changed):
        pass
    s = SyncService(repo, bridge, rides)
    yield s
    bridge.shutdown()


@pytest.fixture
def page(service, qtbot):
    p = SyncPage(service)
    qtbot.addWidget(p)
    return p


@pytest.fixture
def history(repo, service, qtbot):
    p = HistoryPage(repo, service)
    p.errors, p.urls = [], []
    p.show_error, p.open_url = p.errors.append, p.urls.append
    qtbot.addWidget(p)
    return p


def add(repo):
    sid = repo.start_session(Workout("W"), 200)
    repo.add_samples(sid, [Sample(float(t), 200, None, None, 150, None) for t in range(90)])
    repo.finish_session(sid, "completed")
    return sid


def login(qtbot, page):
    page.client_id.setText("1")
    page.client_secret.setText("s")
    page.connect_button.click()
    qtbot.waitUntil(lambda: page.status.text() == "Connected")


def test_login_and_disconnect(qtbot, page):
    assert not page.connect_button.isEnabled() and not page.disconnect_button.isEnabled()
    login(qtbot, page)
    assert page.client_secret.text() == "" and page.message.text() == ""
    page.disconnect_button.click()
    qtbot.waitUntil(lambda: page.status.text() == "Not connected")


def test_cancel_login(qtbot, page):
    page.client_id.setText("1")
    page.client_secret.setText("wait")
    page.connect_button.click()
    assert page.connect_button.text() == "Cancel login"
    page.connect_button.click()
    qtbot.waitUntil(lambda: page.connect_button.text() == "Connect")
    assert page.message.text() == "Login cancelled."


def test_auto_upload_is_off_until_enabled(service, page):
    assert not page.auto.isChecked() and not service.auto_upload
    page.auto.click()
    assert service.auto_upload
    page.auto.click()
    assert not service.auto_upload


def test_storage_failure_is_shown(service, page, monkeypatch):
    def fail(on):
        raise OSError("disk full")

    monkeypatch.setattr(service, "set_auto_upload", fail)
    page.auto.click()
    assert "disk full" in page.message.text()


def test_import(qtbot, repo, service, page, tmp_path):
    sid = add(repo)
    data = fit_bytes(repo.get_session(sid), repo.get_samples(sid))
    repo.delete_session(sid)
    (tmp_path / "a.fit").write_bytes(data)
    (tmp_path / "bad.fit").write_bytes(b"x")
    page.choose_folder = lambda: []
    page.folder_button.click()
    assert not service.importing
    page.choose_folder = lambda: [tmp_path]
    with qtbot.waitSignal(service.import_finished):
        page.folder_button.click()
        assert not page.files_button.isEnabled() and page.cancel_button.isEnabled()
    assert page.progress.text().startswith("Finished: 1 imported, 0 already known")
    assert page.results.count() == 1 and "bad.fit" in page.results.item(0).text()
    assert page.files_button.isEnabled() and len(repo.list_sessions()) == 1


def test_history_upload(qtbot, repo, page, history):
    sid = add(repo)
    history.refresh()
    assert not history.strava_button.isEnabled()
    history.table.selectRow(0)
    assert not history.strava_button.isEnabled() and "Sync" in history.strava_button.toolTip()
    login(qtbot, page)
    assert history.strava_button.isEnabled()
    history.strava_button.click()
    qtbot.waitUntil(lambda: history.strava_button.text() == "View on Strava")
    history.strava_button.click()
    assert history.urls == ["https://www.strava.com/activities/77"]
    repo.save_upload(Upload(sid, "failed", error="bad"))
    history.refresh()
    assert history.strava_button.text() == "Retry upload"
    assert history.strava_button.toolTip() == "bad"
    repo.save_upload(Upload(sid, "queued"))
    history.refresh()
    assert history.strava_button.text() == "Uploading…" and not history.strava_button.isEnabled()
    assert history.errors == []


def test_history_of_imported_ride(qtbot, repo, page, history):
    start = datetime(2025, 3, 1, tzinfo=UTC)
    samples = [Sample(float(t), 200, None, None, 0, None) for t in range(60)]
    ride = ImportedRide(start, start + timedelta(seconds=60), "R", samples)
    sid = repo.import_session(ride, "zwift", "1", "a").session_id
    login(qtbot, page)
    history.refresh()
    history.table.selectRow(0)
    assert history.chart.curves["target"].getData()[0] is None or not len(
        history.chart.curves["target"].getData()[0]
    )
    assert len(history.power_curve.getData()[0]) == 60
    history.confirm_upload = lambda s: False
    history.strava_button.click()
    assert repo.get_upload(sid) is None
    history.confirm_upload = lambda s: True
    history.strava_button.click()
    qtbot.waitUntil(lambda: repo.get_upload(sid) and repo.get_upload(sid).state == "done")


def test_main_window(qtbot, repo, service):
    bridge = TrainerBridge(FakeTrainer)
    controller = RideController(repo, bridge)
    window = MainWindow(repo, bridge, controller, service)
    qtbot.addWidget(window)
    window.sidebar.setCurrentRow(SYNC)
    assert window.stack.currentWidget() is window.sync
    add(repo)
    service.import_finished.emit(ImportSummary())
    assert window.history.table.rowCount() == 1
    controller.shutdown()
    bridge.shutdown()
