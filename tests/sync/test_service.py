import gc
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from pywatts.core.workout import Workout
from pywatts.storage import db, import_files
from pywatts.storage.fit_export import fit_bytes
from pywatts.storage.fit_import import parse_fit
from pywatts.storage.repo import ImportedRide, Repo, Sample, Upload
from pywatts.sync import thread
from pywatts.sync.base import NotConnected, RateLimited, SyncError, Transient
from pywatts.sync.service import SyncService, activity_name
from pywatts.sync.strava import UploadStatus
from pywatts.sync.thread import SyncBridge

START = datetime(2025, 3, 1, 12, tzinfo=UTC)


class FakeRides(QObject):
    session_finished = pyqtSignal(int)
    state_changed = pyqtSignal(object)
    is_active = False

    def set_active(self, on):
        self.is_active = on
        self.state_changed.emit(None)


class FakeStrava:
    def __init__(self):
        self.connected = True
        self.uploads = []  # (name, external id)
        self.answers = []  # for upload and poll; default: done
        self.polled = []

    def _answer(self):
        answer = self.answers.pop(0) if self.answers else UploadStatus("5", "77")
        if isinstance(answer, Exception):
            raise answer
        return answer

    def upload(self, fit, name, external_id):
        parse_fit(fit)
        self.uploads.append((name, external_id))
        return self._answer()

    def poll(self, upload_id):
        self.polled.append(upload_id)
        return self._answer()

    def connect(self, client_id, secret, cancel):
        if not secret:
            raise SyncError("no secret")
        self.connected = True

    def disconnect(self):
        self.connected = False


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(thread, "RETRY_WAITS_S", (0.0, 0.0))
    monkeypatch.setattr(thread, "POLL_S", 0.0)


@pytest.fixture
def repo(tmp_path):
    conn = db.open_db(tmp_path / "t.db")
    yield Repo(conn)
    conn.close()


@pytest.fixture
def strava():
    return FakeStrava()


@pytest.fixture
def rides(qtbot):
    return FakeRides()


@pytest.fixture
def make(qtbot, repo, rides):
    bridges = []

    def build(client):
        bridge = SyncBridge(None if client is None else lambda: client)
        bridges.append(bridge)
        service = SyncService(repo, bridge, rides)
        service.errors = []
        service.error.connect(service.errors.append)
        if client is not None and client.connected:
            qtbot.waitUntil(lambda: service.connected)
        return service

    yield build
    for bridge in bridges:
        bridge.shutdown()


def ride(repo, seconds=90, status="completed", start=None):
    sid = repo.start_session(Workout("W"), 200)
    if start is not None:
        repo._conn.execute(
            "UPDATE sessions SET started_at_utc = ? WHERE id = ?", (start.isoformat(), sid)
        )
    repo.add_samples(sid, [Sample(float(t), 200, None, None, 200, 30.0) for t in range(seconds)])
    if status:
        repo.finish_session(sid, status)
    return sid


def state(repo, sid):
    upload = repo.get_upload(sid)
    return upload and upload.state


def settle(qtbot):
    qtbot.wait(150)


# --- uploads ---


def test_button_upload(qtbot, repo, strava, make):
    strava.answers = [UploadStatus("5"), UploadStatus("5"), UploadStatus("5", "77")]
    service = make(strava)
    sid = ride(repo, status="aborted")
    service.upload(sid)
    service.upload(sid)  # double click
    qtbot.waitUntil(lambda: state(repo, sid) == "done")
    assert repo.get_upload(sid) == Upload(sid, "done", "5", "77")
    service.upload(sid)
    settle(qtbot)
    assert len(strava.uploads) == 1 and strava.polled == ["5", "5"] and service.errors == []
    name, external = strava.uploads[0]
    assert name == "W with pyWatts ⚡" and external.startswith("pywatts-")


def test_activity_name(repo):
    session = repo.get_session(ride(repo))
    assert activity_name(session) == "W with pyWatts ⚡"
    assert activity_name(replace(session, workout=Workout(" "))) == "Ride with pyWatts ⚡"
    assert activity_name(replace(session, source="wahoo")) == "W"


def test_auto_upload_rules(qtbot, repo, strava, rides, make):
    service = make(strava)
    off = ride(repo)
    rides.session_finished.emit(off)
    service.set_auto_upload(True)
    rejected = [off]
    for sid in (ride(repo, 30), ride(repo, status="aborted"), ride(repo, status=None)):
        rides.session_finished.emit(sid)
        rejected.append(sid)
    samples = [Sample(float(t), 1, None, None, 0, None) for t in range(90)]
    old = ImportedRide(START, START + timedelta(seconds=90), "R", samples)
    imported = repo.import_session(old, "wahoo", "1", "a").session_id
    rides.session_finished.emit(imported)
    good = ride(repo)
    rides.session_finished.emit(good)
    qtbot.waitUntil(lambda: state(repo, good) == "done")
    settle(qtbot)
    assert [state(repo, s) for s in [*rejected, imported]] == [None] * 5
    assert service.needs_confirmation(repo.get_session(imported))
    assert not service.needs_confirmation(repo.get_session(good))


def test_not_connected(qtbot, repo, strava, make):
    strava.connected = False
    service = make(strava)
    sid = ride(repo)
    service.upload(sid)
    assert service.errors and state(repo, sid) is None
    service.connect_strava("1", "")
    qtbot.waitUntil(lambda: "no secret" in service.errors)
    service.connect_strava("1", "s")
    qtbot.waitUntil(lambda: service.connected)
    service.disconnect_strava()
    qtbot.waitUntil(lambda: not service.connected)


def test_short_outage_is_retried(qtbot, repo, strava, make):
    strava.answers = [Transient("a"), Transient("b")]
    service = make(strava)
    sid = ride(repo)
    service.upload(sid)
    qtbot.waitUntil(lambda: state(repo, sid) == "done")
    assert len(strava.uploads) == 3 and service.errors == []


def test_queue_survives_restart(qtbot, repo, strava, make):
    strava.answers = [Transient("down")] * 3
    service = make(strava)
    sid = ride(repo)
    service.upload(sid)
    qtbot.waitUntil(lambda: bool(service.errors))
    assert state(repo, sid) == "queued"
    processing = ride(repo)
    repo.save_upload(Upload(processing, "processing", "9"))
    second = FakeStrava()
    make(second)
    qtbot.waitUntil(lambda: state(repo, sid) == state(repo, processing) == "done")
    assert len(second.uploads) == 1 and second.polled == ["9"]


def test_rate_limit_blocks_further_uploads(qtbot, repo, strava, make):
    strava.answers = [RateLimited("limit", 900)]
    service = make(strava)
    one, two = ride(repo), ride(repo)
    service.upload(one)
    service.upload(two)
    qtbot.waitUntil(lambda: len(service.errors) == 2)
    assert len(strava.uploads) == 1
    assert state(repo, one) == state(repo, two) == "queued"


def test_login_lost(qtbot, repo, strava, make):
    strava.answers = [NotConnected("reconnect")]
    service = make(strava)
    sid = ride(repo)
    service.upload(sid)
    qtbot.waitUntil(lambda: not service.connected)
    assert state(repo, sid) == "queued"
    service.connect_strava("1", "s")
    qtbot.waitUntil(lambda: state(repo, sid) == "done")


def test_failed_upload_can_be_repeated(qtbot, repo, strava, make):
    strava.answers = [UploadStatus("5", error="bad")]
    service = make(strava)
    sid = ride(repo)
    service.upload(sid)
    qtbot.waitUntil(lambda: state(repo, sid) == "failed")
    assert repo.get_upload(sid).error == "bad"
    service.upload(sid)
    qtbot.waitUntil(lambda: state(repo, sid) == "done")
    assert len(strava.uploads) == 2


def test_ride_without_samples_fails(qtbot, repo, strava, make):
    service = make(strava)
    sid = ride(repo, 0)
    service.upload(sid)
    qtbot.waitUntil(lambda: state(repo, sid) == "failed")


def test_deleted_meanwhile(qtbot, repo, strava, rides, make):
    service = make(strava)
    sid = ride(repo)
    rides.set_active(True)
    service.upload(sid)
    repo.delete_session(sid)
    rides.set_active(False)
    qtbot.waitUntil(lambda: bool(strava.uploads))
    settle(qtbot)
    assert service.errors == [] and repo.list_uploads() == []


# --- files ---


def write_rides(repo, folder, count):
    for i in range(count):
        sid = ride(repo, start=START + timedelta(days=i))
        row = repo.get_session(sid)
        (folder / f"{i}.fit").write_bytes(fit_bytes(row, repo.get_samples(sid)))
        repo.delete_session(sid)


def test_import(qtbot, repo, strava, make, tmp_path):
    write_rides(repo, tmp_path, 3)
    (tmp_path / "bad.fit").write_bytes(b"x")
    (tmp_path / "a.gpx").write_bytes(b"x")
    service = make(strava)
    service.set_auto_upload(True)
    with qtbot.waitSignal(service.import_finished) as done:
        assert service.import_files([tmp_path])
        assert not service.import_files([tmp_path])
    s = done.args[0]
    assert (s.imported, s.duplicates, s.skipped, len(s.errors)) == (3, 0, 1, 1)
    with qtbot.waitSignal(service.import_finished) as done:
        assert service.import_files([tmp_path])
    assert (done.args[0].imported, done.args[0].duplicates) == (0, 3)
    assert len(repo.list_sessions()) == 3 and strava.uploads == []


def test_failing_database_does_not_block_the_import(qtbot, repo, make, tmp_path, monkeypatch):
    write_rides(repo, tmp_path, 2)
    service = make(None)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(repo, "import_session", fail)
    with qtbot.waitSignal(service.import_finished) as done:
        service.import_files([tmp_path])
    assert len(done.args[0].errors) == 2


def test_nothing_runs_during_a_ride(qtbot, repo, strava, rides, make, tmp_path):
    write_rides(repo, tmp_path, 1)
    service = make(strava)
    sid = ride(repo)
    rides.set_active(True)
    service.upload(sid)
    with qtbot.waitSignal(service.import_finished):
        service.import_files([tmp_path])
        settle(qtbot)
        assert strava.uploads == [] and len(repo.list_sessions()) == 1
        rides.set_active(False)
    assert len(repo.list_sessions()) == 2 and state(repo, sid) == "done"


def test_decoding_waits_for_the_ride_to_end(qtbot, repo, rides, make, tmp_path, monkeypatch):
    write_rides(repo, tmp_path, 2)
    decoded = []
    real = import_files.read_bytes

    def spy(name, data):
        decoded.append(name)
        return real(name, data)

    monkeypatch.setattr(import_files, "read_bytes", spy)
    service = make(None)
    rides.set_active(True)
    with qtbot.waitSignal(service.import_finished):
        service.import_files([tmp_path])
        settle(qtbot)
        assert decoded == []
        rides.set_active(False)
    assert len(decoded) == 2


# --- fake mode, shutdown ---


def test_garbage_is_only_collected_on_the_gui_thread(qtbot, make):
    assert gc.isenabled()
    first, second = make(None), make(None)
    assert not gc.isenabled()
    threads = set()

    def note(phase, info):
        threads.add(threading.current_thread())

    gc.callbacks.append(note)
    try:
        done = threading.Event()

        def allocate():
            for _ in range(50_000):
                cycle = []
                cycle.append(cycle)
            done.set()

        first._bridge._worker.submit(allocate)
        qtbot.waitUntil(done.is_set)
        first._bridge._gc.collect()
    finally:
        gc.callbacks.remove(note)
    assert threads == {threading.main_thread()}
    first._bridge.shutdown()
    first._bridge.shutdown()
    assert not gc.isenabled()
    second._bridge.shutdown()
    assert gc.isenabled()


def test_without_network(qtbot, repo, make):
    service = make(None)
    sid = ride(repo)
    service.upload(sid)
    service.connect_strava("1", "s")
    qtbot.waitUntil(lambda: len(service.errors) == 2)
    assert not service.connected and state(repo, sid) is None


def test_shutdown_is_quick_and_keeps_the_queue(qtbot, repo, strava, make, rides):
    release = threading.Event()
    service = make(strava)
    rides.set_active(True)
    sid = ride(repo)
    service.upload(sid)
    settle(qtbot)
    started = time.monotonic()
    service._bridge.shutdown()
    service._bridge.shutdown()
    assert time.monotonic() - started < 2
    release.set()
    assert state(repo, sid) == "queued" and strava.uploads == []
