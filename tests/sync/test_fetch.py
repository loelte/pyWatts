"""Rides of services: worker and rules together, with a simulated service."""

import io
import threading
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from pywatts.core.workout import Workout
from pywatts.storage import db
from pywatts.storage.fit_export import fit_bytes
from pywatts.storage.repo import SERVICES, Repo, Sample, service_key
from pywatts.sync import service as service_module
from pywatts.sync import thread
from pywatts.sync.base import NotConnected, Page, RateLimited, RemoteRide, SyncError, Transient
from pywatts.sync.service import SyncService
from pywatts.sync.strava import UploadStatus
from pywatts.sync.thread import SyncBridge

NOW = datetime.now(UTC).replace(microsecond=0)


class FakeRides(QObject):
    session_finished = pyqtSignal(int)
    state_changed = pyqtSignal(object)
    is_active = False

    def set_active(self, on):
        self.is_active = on
        self.state_changed.emit(None)


class FakeSource:
    def __init__(self, connected=True):
        self.connected = connected
        self.rides = {}  # id: (RemoteRide, bytes | None | Exception)
        self.page_errors = []
        self.per_page = 10
        self.pages, self.downloads, self.since = [], [], []
        self.hold = threading.Event()  # blocks downloads while cleared
        self.hold.set()

    def add(self, number, days, data=None, **kw):
        at = NOW - timedelta(days=days)
        ride = RemoteRide(str(number), at, kw.pop("title", ""), kw.pop("cycling", True))
        self.rides[str(number)] = (ride, fit(at) if data is None else data)
        return ride

    def login(self, ok=True):
        if not ok:
            raise SyncError("wrong key")
        self.connected = True

    def disconnect(self):
        self.connected = False

    def page(self, number, since):
        self.pages.append(number)
        self.since.append(since)
        if self.page_errors:
            raise self.page_errors.pop(0)
        rides = sorted((r for r, _ in self.rides.values()), key=lambda r: r.started_at_utc)[::-1]
        first = (number - 1) * self.per_page
        return Page(rides[first : first + self.per_page], len(rides) > first + self.per_page)

    def download(self, ride):
        self.downloads.append(ride.external_id)
        self.hold.wait(5)
        data = self.rides[ride.external_id][1]
        if isinstance(data, Exception):
            raise data
        return data or None


_fits = {}


def fit(at, seconds=90):
    """A FIT file of a ride at that time."""
    if (at, seconds) not in _fits:
        conn = db.open_db(":memory:", ":memory:")
        repo = Repo(conn)
        sid = record(repo, at, seconds)
        _fits[at, seconds] = fit_bytes(repo.get_session(sid), repo.get_samples(sid))
        conn.close()
    return _fits[at, seconds]


def record(repo, at, seconds=90):
    sid = repo.start_session(Workout("W"), 200)
    repo.add_samples(sid, [Sample(float(t), 200, None, None, 200, 30.0) for t in range(seconds)])
    repo.finish_session(sid, "completed")
    repo._conn.execute(
        "UPDATE sessions SET started_at_utc = ?, ended_at_utc = ? WHERE id = ?",
        (at.isoformat(), (at + timedelta(seconds=seconds)).isoformat(), sid),
    )
    repo._conn.commit()
    return sid


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(service_module, "START_MS", 0)


@pytest.fixture
def repo(tmp_path):
    conn = db.open_db(tmp_path / "t.db")
    yield Repo(conn)
    conn.close()


@pytest.fixture
def rides(qtbot):
    return FakeRides()


@pytest.fixture
def source():
    return FakeSource()


class FakeStrava:
    connected = True

    def __init__(self):
        self.uploads = []

    def upload(self, fit, name, external_id):
        self.uploads.append(name)
        return UploadStatus("5", "77")


@pytest.fixture
def make(qtbot, repo, rides):
    bridges = []

    def build(source, strava=None, name="wahoo"):
        bridge = SyncBridge(
            None if strava is None else lambda: strava, sources={name: lambda: source}
        )
        bridges.append((bridge, source))
        service = SyncService(repo, bridge, rides)
        service.finished = []
        service.logins = []
        service.fetch_finished.connect(lambda n, s: service.finished.append((n, s)))
        service.login_finished.connect(lambda n, e: service.logins.append((n, e)))
        return service

    yield build
    for bridge, source in bridges:
        source.hold.set()
        bridge.shutdown()


def checked(repo, name="wahoo"):
    return repo.get_setting(service_key(name, "checked"))


def done(qtbot, service, name="wahoo"):
    """Wait for a fetch that has begun or is about to."""
    qtbot.waitUntil(lambda: service.source(name).summary is not None)
    qtbot.waitUntil(lambda: service.source(name).job is None)


def again(qtbot, service, **kw):
    service.fetch("wahoo", **kw)
    done(qtbot, service)
    qtbot.wait(20)
    done(qtbot, service)


def test_new_rides_come_by_themselves(qtbot, repo, source, make):
    source.add(1, 1, title="  Evening \n ride ")
    source.add(2, 40)
    source.add(3, 91)  # before the first fetch
    service = make(source)
    done(qtbot, service)
    sessions = repo.list_sessions()
    assert [s.workout.name for s in sessions] == ["Evening ride", "Imported ride"]
    assert {s.source for s in sessions} == {"wahoo"} and source.downloads == ["1", "2"]
    assert [(n, s.imported) for n, s in service.finished] == [("wahoo", 2)]
    again(qtbot, service)
    assert source.downloads == ["1", "2"] and len(service.finished) == 1


def test_not_at_start_if_switched_off(qtbot, repo, source, make):
    source.add(1, 1)
    repo.set_setting(service_key("wahoo", "auto"), "0")
    service = make(source)
    qtbot.waitUntil(lambda: service.source("wahoo").connected)
    qtbot.wait(100)
    assert source.pages == []
    again(qtbot, service)
    assert len(repo.list_sessions()) == 1


def test_not_connected(qtbot, repo, make):
    source = FakeSource(connected=False)
    source.add(1, 1)
    service = make(source)
    service.fetch("wahoo")
    qtbot.wait(100)
    assert source.pages == []
    service.log_in("wahoo", lambda c: c.login(False))
    qtbot.waitUntil(lambda: service.logins == [("wahoo", "wrong key")])
    service.log_in("wahoo", lambda c: c.login())
    done(qtbot, service)
    assert len(repo.list_sessions()) == 1 and service.logins[-1] == ("wahoo", "")


def test_late_uploads_are_found(qtbot, repo, source, make):
    source.add(1, 1)
    service = make(source)
    done(qtbot, service)
    source.add(2, 13)
    source.add(3, 16)
    again(qtbot, service)
    assert source.downloads == ["1", "2"]
    assert NOW - timedelta(days=14, minutes=1) < source.since[-1] < NOW - timedelta(days=13)


def test_everything(qtbot, repo, source, make):
    source.per_page = 2
    for i in range(5):
        source.add(i, i * 100 + 1)
    service = make(source)
    done(qtbot, service)
    assert source.downloads == ["0"]
    source.page_errors = [RateLimited("limit", 900)]
    again(qtbot, service, everything=True)
    assert repo.get_setting(service_key("wahoo", "full")) == "1"
    second = FakeSource()
    second.per_page, second.rides = 2, source.rides
    repo.set_setting(service_key("wahoo", "auto"), "0")
    done(qtbot, make(second))  # goes on after a restart
    assert second.downloads == ["1", "2", "3", "4"] and len(repo.list_sessions()) == 5
    assert repo.get_setting(service_key("wahoo", "full")) == ""


def test_everything_while_another_check_runs(qtbot, repo, source, make):
    source.add(1, 1)
    source.add(2, 300)
    source.hold.clear()
    service = make(source)
    qtbot.waitUntil(lambda: source.downloads == ["1"])
    service.fetch("wahoo", everything=True)
    service.fetch("wahoo")
    source.hold.set()
    qtbot.waitUntil(lambda: len(repo.list_sessions()) == 2)
    done(qtbot, service)
    assert source.downloads == ["1", "2"]


def test_limit_then_resume(qtbot, repo, source, make):
    source.add(1, 1)
    source.add(2, 2, data=RateLimited("limit reached", 900))
    service = make(source)
    done(qtbot, service)
    state = service.source("wahoo")
    assert state.result == "limit reached" and state.retry_at > NOW + timedelta(seconds=800)
    assert len(repo.list_sessions()) == 1 and checked(repo) == ""
    service.resume_fetches()  # too early
    assert state.job is None
    source.add(2, 2)
    state.retry_at = NOW
    service.resume_fetches()
    done(qtbot, service)
    assert len(repo.list_sessions()) == 2 and source.downloads == ["1", "2", "2"]
    assert checked(repo) and state.retry_at is None


def test_offline_start(qtbot, repo, rides, source, make):
    source.add(1, 1)
    source.page_errors = [Transient("offline")]
    service = make(source)
    done(qtbot, service)
    assert service.source("wahoo").result == "offline"
    rides.set_active(True)
    service.resume_fetches()
    assert service.source("wahoo").job is None  # not during a ride
    rides.set_active(False)
    service.resume_fetches()
    done(qtbot, service)
    assert len(repo.list_sessions()) == 1


def test_login_lost(qtbot, repo, source, make):
    source.page_errors = [NotConnected("log in again")]
    source.add(1, 1)
    service = make(source)
    state = service.source("wahoo")
    qtbot.waitUntil(lambda: state.error == "log in again")
    done(qtbot, service)
    assert not state.connected and state.retry_at is None
    # A first step does not count, although a login is still stored.
    service.log_in("wahoo", lambda c: None, final=False)
    qtbot.waitUntil(lambda: len(service.logins) == 1)
    assert not state.connected
    service.log_in("wahoo", lambda c: c.login())
    qtbot.waitUntil(lambda: len(repo.list_sessions()) == 1)


def test_one_bad_ride_does_not_stop_the_others(qtbot, repo, source, make):
    source.add(1, 1, data=SyncError("odd"))
    source.add(2, 2, data=b"junk")
    source.add(3, 3, cycling=False)
    source.add(4, 4, data=b"")  # no file yet
    source.add(5, 5)
    service = make(source)
    done(qtbot, service)
    s = service.source("wahoo").summary
    assert (s.imported, s.skipped, len(s.errors)) == (1, 1, 1)
    assert service.source("wahoo").result == "odd" and checked(repo)
    assert "3" not in source.downloads
    source.downloads.clear()
    again(qtbot, service)
    assert source.downloads == ["1", "4"]  # those that may work later


def test_unexpected_failure(qtbot, repo, source, make):
    source.page_errors = [KeyError("x")]
    service = make(source)
    done(qtbot, service)
    state = service.source("wahoo")
    assert "KeyError" in state.result and state.retry_at is None and checked(repo) == ""


def test_service_that_ignores_the_page(qtbot, repo, source, make):
    source.add(1, 1)
    source.page = lambda n, since: Page([source.rides["1"][0]], True)
    service = make(source)
    done(qtbot, service)
    assert len(repo.list_sessions()) == 1 and checked(repo)


def test_zip(qtbot, repo, make):
    source = FakeSource()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("readme.txt", "x")
        z.writestr("1_ACTIVITY.fit", fit(NOW - timedelta(days=1)))
    source.add(1, 1, data=buffer.getvalue())
    source.add(2, 2, data=b"PK junk")
    empty = io.BytesIO()
    zipfile.ZipFile(empty, "w").close()
    source.add(3, 3, data=empty.getvalue())
    service = make(source, name="garmin")
    done(qtbot, service, "garmin")
    assert [s.source for s in repo.list_sessions()] == ["garmin"]
    assert len(service.source("garmin").summary.errors) == 2


def test_known_rides(qtbot, repo, source, make):
    at = NOW - timedelta(days=1)
    record(repo, at)  # ridden with pyWatts, recorded by a Wahoo device too
    source.add(1, 1)
    gone = source.add(2, 2)
    service = make(source)
    done(qtbot, service)
    assert service.source("wahoo").summary.duplicates == 1
    assert gone.external_id in source.downloads and len(repo.list_sessions()) == 2
    (sid,) = repo.service_sessions("wahoo")
    repo.delete_session(sid)
    again(qtbot, service)
    assert len(repo.list_sessions()) == 1  # deleted rides stay away


def test_ride_starts_in_the_middle(qtbot, repo, rides, source, make, monkeypatch):
    decoded = []
    real = thread.read_archive
    monkeypatch.setattr(thread, "read_archive", lambda n, d: decoded.append(n) or real(n, d))
    source.add(1, 1)
    source.add(2, 2)
    source.hold.clear()
    service = make(source)
    qtbot.waitUntil(lambda: source.downloads == ["1"])
    rides.set_active(True)
    source.hold.set()
    qtbot.wait(150)
    assert decoded == [] and source.downloads == ["1"]
    rides.set_active(False)
    done(qtbot, service)
    assert len(decoded) == 2 and len(repo.list_sessions()) == 2


@pytest.mark.parametrize("delete", [True, False])
def test_disconnect_in_the_middle(qtbot, repo, source, make, delete):
    source.add(1, 1)
    service = make(source)
    done(qtbot, service)
    service.set_shared_with_ai("wahoo", True)
    source.add(2, 2)
    source.add(3, 3)
    source.hold.clear()
    service.fetch("wahoo")
    qtbot.waitUntil(lambda: "2" in source.downloads)
    service.disconnect_source("wahoo", delete)
    source.hold.set()
    qtbot.waitUntil(lambda: not service.source("wahoo").connected)
    qtbot.waitUntil(lambda: service.source("wahoo").job is None)
    assert "3" not in source.downloads
    assert len(repo.list_sessions()) == (0 if delete else 1)
    assert len(service.finished) == (2 if delete else 1)  # pages are told
    for what in ("since", "checked", "full", "ai"):
        assert repo.get_setting(service_key("wahoo", what)) == ""
    assert service.logins == []


def test_reconnect_after_deleting(qtbot, repo, source, make):
    source.add(1, 1)
    service = make(source)
    done(qtbot, service)
    service.disconnect_source("wahoo", True)
    qtbot.waitUntil(lambda: not service.source("wahoo").connected)
    assert repo.list_sessions() == []
    service.log_in("wahoo", lambda c: c.login())
    qtbot.waitUntil(lambda: len(repo.list_sessions()) == 1)


def test_failing_database_does_not_block(qtbot, repo, source, make, monkeypatch):
    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(repo, "import_session", fail)
    source.add(1, 1)
    source.add(2, 2)
    service = make(source)
    done(qtbot, service)
    assert len(service.source("wahoo").summary.errors) == 2


def test_fetched_rides_are_not_uploaded(qtbot, repo, source, make):
    strava = FakeStrava()
    repo.set_setting("strava_auto", "1")
    source.add(1, 1)
    service = make(source, strava)
    done(qtbot, service)
    qtbot.wait(100)
    assert strava.uploads == [] and service.needs_confirmation(repo.list_sessions()[0])


def test_without_network(qtbot, repo, rides):
    bridge = SyncBridge(None)
    service = SyncService(repo, bridge, rides)
    errors = []
    service.login_finished.connect(lambda n, e: errors.append(e))
    service.log_in("wahoo", lambda c: c.login())
    qtbot.waitUntil(lambda: len(errors) == 1)
    assert "simulated" in errors[0] and not service.source("wahoo").connected
    bridge.shutdown()


def test_shutdown_in_the_middle(qtbot, repo, rides, source, make):
    source.add(1, 1)
    rides.set_active(True)
    service = make(source)
    qtbot.waitUntil(lambda: service.source("wahoo").job is not None)
    service._bridge.shutdown()
    assert service._bridge._worker.join(0)


# --- storage ---


def test_forget_service(repo):
    from pywatts.storage.import_files import read_bytes

    at = NOW - timedelta(days=1)
    api = read_bytes("a", fit(at))
    sid = repo.import_session(api.ride, "wahoo", "1", api.sha256).session_id
    # The same ride as a file, and another one from a file.
    other = read_bytes("b", fit(at, 80))
    assert repo.import_session(other.ride, "wahoo", other.external_id, "x").result == "duplicate"
    third = read_bytes("c", fit(at - timedelta(days=3)))
    kept = repo.import_session(third.ride, "wahoo", third.external_id, third.sha256).session_id
    repo.skip_import("wahoo", "9")
    repo.skip_import("garmin", "9")
    assert repo.service_sessions("wahoo") == {sid} == repo.hidden_from_ai()
    repo.set_setting(service_key("wahoo", "ai"), "1")
    assert repo.hidden_from_ai() == set()
    assert repo.forget_service("wahoo") == 1
    assert [s.id for s in repo.list_sessions()] == [kept]
    assert repo.known_imports("wahoo") == {third.external_id}
    assert repo.known_imports("garmin") == {"9"}
    assert repo.import_session(other.ride, "wahoo", other.external_id, "x").result == "imported"
    assert SERVICES == ("wahoo", "garmin")
