"""Sync worker thread + Qt bridge. No database access here: results go to the GUI thread."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from queue import Queue

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from pywatts.storage.import_files import FileResult, read_archive, read_files
from pywatts.storage.repo import Sample, SessionRow, Upload
from pywatts.sync.base import (
    ActivitySource,
    NotConnected,
    RateLimited,
    RemoteRide,
    SyncError,
    Transient,
)
from pywatts.sync.gui_gc import GuiGc
from pywatts.sync.strava import StravaClient, UploadStatus

log = logging.getLogger(__name__)

RETRY_WAITS_S = (2.0, 8.0)
POLL_S = 2.0
POLL_COUNT = 30
MAX_PAGES = 500


@dataclass(frozen=True)
class UploadJob:
    session: SessionRow
    samples: list[Sample]
    name: str
    external_id: str
    upload_id: str | None = None  # already accepted, only poll


@dataclass(frozen=True)
class FetchJob:
    """Load the rides of a service that are not known."""

    source: str
    known: frozenset[str]  # external ids
    since: datetime | None  # None: all
    cancel: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class FetchedRide:
    job: FetchJob
    ride: RemoteRide
    result: FileResult


@dataclass(frozen=True)
class FetchEnd:
    job: FetchJob
    complete: bool  # everything was looked at
    error: str = ""
    retry_after_s: float | None = None  # None: repeating does not help


@dataclass(frozen=True)
class SourceState:
    source: str
    connected: bool
    error: str = ""
    login: bool = False  # answers log_in


class _Stopped(Exception):
    pass


class _Cancelled(Exception):
    pass


class _Worker:
    """No Qt. Hands results to `post`, which is never called once closed."""

    def __init__(
        self,
        factory: Callable[[], StravaClient] | None,
        post: Callable[[str, object], object],
        sources: Mapping[str, Callable[[], ActivitySource]],
    ) -> None:
        self._factory = factory
        self._factories = dict(sources)
        self._sources: dict[str, ActivitySource] = {}
        self._denied: set[str] = set()  # their login is stored but refused
        self._post = post
        self._lock = threading.Lock()
        self._closed = False
        self._client: StravaClient | None = None
        self._blocked_until = 0.0  # rate limit
        self._jobs: Queue[Callable[[], None] | None] = Queue()
        self.stop = threading.Event()
        self.cancel_login = threading.Event()
        self.cancel_import = threading.Event()
        self.may_run = threading.Event()  # cleared during a ride
        self.may_run.set()
        self.acknowledged = threading.Semaphore(0)
        self._thread = threading.Thread(target=self._main, name="sync", daemon=True)
        self._thread.start()

    def submit(self, job: Callable[[], None]) -> None:
        self._jobs.put(job)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._post = lambda kind, payload: None  # breaks the cycle with the bridge
        self.stop.set()
        self.cancel_login.set()
        self._jobs.put(None)

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def post(self, kind: str, payload: object) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._post(kind, payload)
            except RuntimeError:  # C++ object deleted
                self._closed = True

    # --- worker thread ---

    def _main(self) -> None:
        self._report_connection()
        for name in self._factories:
            self._report_source(name)
        while (job := self._jobs.get()) is not None and not self.stop.is_set():
            try:
                job()
            except _Stopped:
                break
            except SyncError as e:
                self.post("error", str(e))
            except Exception as e:
                log.exception("sync job failed")
                self.post("error", f"Unexpected error: {type(e).__name__}")

    def _wait(self, seconds: float) -> None:
        if self.stop.wait(seconds):
            raise _Stopped

    def _gate(self) -> None:
        """Nothing heavy during a ride: it would disturb the 1 Hz control."""
        while not self.may_run.wait(0.2):
            if self.stop.is_set():
                raise _Stopped
        if self.stop.is_set():
            raise _Stopped

    def _need_client(self) -> StravaClient:
        if self._client is None:
            if self._factory is None:
                raise SyncError("Strava is not available with the simulated trainer.")
            self._client = self._factory()
        return self._client

    def _report_connection(self) -> None:
        connected = False
        if self._factory is not None:
            try:
                connected = self._need_client().connected
            except SyncError as e:
                self.post("error", str(e))
            except Exception as e:
                log.exception("Strava client failed")
                self.post("error", f"Strava unavailable: {type(e).__name__}")
        self.post("connected", connected)

    def connect(self, client_id: str, client_secret: str) -> None:
        try:
            self._need_client().connect(client_id, client_secret, self.cancel_login)
        finally:
            self._report_connection()

    def disconnect(self) -> None:
        try:
            self._need_client().disconnect()
        finally:
            self._report_connection()

    # --- sources ---

    def _need_source(self, name: str) -> ActivitySource:
        if name not in self._sources:
            if name not in self._factories:
                raise SyncError("Not available with the simulated trainer.")
            self._sources[name] = self._factories[name]()
        return self._sources[name]

    def _report_source(self, name: str, error: str = "", login: bool = False) -> None:
        connected = False
        try:
            connected = self._need_source(name).connected and name not in self._denied
        except SyncError as e:
            error = error or str(e)
        except Exception as e:
            log.exception("%s client failed", name)
            error = error or f"Unavailable: {type(e).__name__}"
        self.post("source", SourceState(name, connected, error, login))

    def account(
        self, name: str, action: Callable[[ActivitySource], object], login: bool, final: bool
    ) -> None:
        """Log in or out; always reports the state."""
        error = ""
        try:
            action(self._need_source(name))
            if final:
                self._denied.discard(name)
        except SyncError as e:
            error = str(e)
        except Exception as e:
            log.exception("%s account failed", name)
            error = f"Unexpected error: {type(e).__name__}"
        self._report_source(name, error, login)

    def fetch(self, job: FetchJob) -> None:
        end = FetchEnd(job, False)
        try:
            errors = self._fetch(job)
            end = FetchEnd(job, True, errors)
        except _Cancelled:
            pass
        except RateLimited as e:
            end = FetchEnd(job, False, str(e), e.retry_after_s)
        except Transient as e:
            end = FetchEnd(job, False, str(e), 0.0)
        except NotConnected as e:
            end = FetchEnd(job, False, str(e))
            self._denied.add(job.source)
            self.post("source", SourceState(job.source, False, str(e)))
        except SyncError as e:
            end = FetchEnd(job, False, str(e))
        except _Stopped:
            raise
        except Exception as e:
            log.exception("fetching from %s failed", job.source)
            end = FetchEnd(job, False, f"Unexpected error: {type(e).__name__}")
        finally:
            self.post("fetch_end", end)

    def _check(self, job: FetchJob) -> None:
        self._gate()
        if job.cancel.is_set():
            raise _Cancelled

    def _fetch(self, job: FetchJob) -> str:
        """Returns the last error of a single ride, which does not stop the others."""
        self._check(job)
        source = self._need_source(job.source)
        seen: set[str] = set()
        error = ""
        for number in range(1, MAX_PAGES + 1):
            self._check(job)
            page = source.page(number, job.since)
            new = [r for r in page.rides if r.external_id not in seen]
            seen.update(r.external_id for r in new)
            for ride in new:
                at = ride.started_at_utc
                early = job.since is not None and at is not None and at < job.since
                if early or ride.external_id in job.known:
                    continue
                self._check(job)
                try:
                    result = self._download(job, source, ride)
                except (Transient, NotConnected):
                    raise
                except SyncError as e:
                    error = str(e)
                    continue
                if result is not None:
                    self.post("fetched", FetchedRide(job, ride, result))
                    self._await_acknowledge()
            # A service that ignores the page number would go on for ever.
            if not page.more or (page.rides and not new):
                break
        return error

    def _download(
        self, job: FetchJob, source: ActivitySource, ride: RemoteRide
    ) -> FileResult | None:
        if ride.cycling is False:
            return FileResult(ride.external_id, skipped=True, error="not a cycling activity")
        data = source.download(ride)
        if data is None:
            return None
        self._check(job)  # a ride may have started during the download
        return read_archive(f"{job.source} {ride.external_id}", data)

    # --- files ---

    def import_files(self, paths: list[Path]) -> None:
        try:
            results = read_files(paths)
            while True:
                self._gate()  # before the next file is decoded
                if self.cancel_import.is_set() or (result := next(results, None)) is None:
                    break
                self.post("file", result)
                self._await_acknowledge()
        finally:
            self.post("import_finished", None)

    def _await_acknowledge(self) -> None:
        """One at a time, so that decoded rides do not pile up in memory."""
        while not self.acknowledged.acquire(timeout=0.2):
            if self.stop.is_set():
                raise _Stopped

    def upload(self, job: UploadJob) -> None:
        sid = job.session.id
        upload_id = job.upload_id
        try:
            self._gate()
            if time.monotonic() < self._blocked_until:
                raise Transient("Waiting for Strava's request limit.")
            client = self._need_client()
            if upload_id is None:
                from pywatts.storage.fit_export import fit_bytes  # slow to load

                data = fit_bytes(job.session, job.samples)
                status = self._retry(lambda: client.upload(data, job.name, job.external_id))
            else:
                status = UploadStatus(upload_id)
            for _ in range(POLL_COUNT):
                upload_id = status.upload_id or upload_id
                if status.activity_id:
                    done = Upload(sid, "done", upload_id, status.activity_id)
                    self.post("upload", done)
                    return
                if status.error:
                    self.post("upload", Upload(sid, "failed", error=status.error))
                    return
                assert upload_id is not None
                self.post("upload_progress", Upload(sid, "processing", upload_id))
                self._wait(POLL_S)
                status = self._retry(partial(client.poll, upload_id))
            raise Transient("Strava is still processing the ride.")
        except (Transient, NotConnected) as e:
            if isinstance(e, RateLimited):
                self._blocked_until = time.monotonic() + e.retry_after_s
            state = "processing" if upload_id else "queued"
            self.post("upload", Upload(sid, state, upload_id, error=str(e)))
            if isinstance(e, NotConnected):
                self.post("connected", False)
        except (SyncError, ValueError) as e:
            self.post("upload", Upload(sid, "failed", error=str(e)))
        except _Stopped:
            raise
        except Exception as e:
            log.exception("upload failed")
            text = f"Unexpected error: {type(e).__name__}"
            self.post("upload", Upload(sid, "failed", error=text))

    def _retry(self, action: Callable[[], UploadStatus]) -> UploadStatus:
        # A lost response is harmless: the repeated upload is answered as a duplicate.
        for wait in RETRY_WAITS_S:
            try:
                return action()
            except RateLimited:
                raise
            except Transient:
                self._wait(wait)
        return action()


class SyncBridge(QObject):
    """Methods are called from the GUI thread and never block (except shutdown, bounded).

    Every upload() ends with exactly one upload_finished, unless the app shuts down.
    Every fetch() ends with exactly one fetch_finished, unless the app shuts down.
    Every file_ready and ride_fetched must be answered with acknowledge().
    """

    connected_changed = pyqtSignal(bool)
    upload_progress = pyqtSignal(object)  # Upload
    upload_finished = pyqtSignal(object)  # Upload
    file_ready = pyqtSignal(object)  # FileResult
    import_finished = pyqtSignal()
    source_changed = pyqtSignal(object)  # SourceState
    ride_fetched = pyqtSignal(object)  # FetchedRide
    fetch_finished = pyqtSignal(object)  # FetchEnd
    error = pyqtSignal(str)

    _posted = pyqtSignal(str, object)

    def __init__(
        self,
        factory: Callable[[], StravaClient] | None,
        parent: QObject | None = None,
        sources: Mapping[str, Callable[[], ActivitySource]] | None = None,
    ) -> None:
        """Factories run on the worker thread. factory: of Strava, None = no network."""
        super().__init__(parent)
        self.connected = False
        self._gc = GuiGc(self)  # before the worker starts
        self._posted.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)
        worker = self._worker = _Worker(factory, self._posted.emit, sources or {})
        # Deleted by Qt without shutdown().
        self._on_destroyed: Callable[[], None] | None = lambda: worker.close()
        self.destroyed.connect(self._on_destroyed)

    @pyqtSlot(str, object)
    def _dispatch(self, kind: str, payload: object) -> None:
        if kind == "connected":
            self.connected = bool(payload)
            self.connected_changed.emit(self.connected)
        elif kind == "upload":
            self.upload_finished.emit(payload)
        elif kind == "upload_progress":
            self.upload_progress.emit(payload)
        elif kind == "file":
            self.file_ready.emit(payload)
        elif kind == "import_finished":
            self.import_finished.emit()
        elif kind == "source":
            self.source_changed.emit(payload)
        elif kind == "fetched":
            self.ride_fetched.emit(payload)
        elif kind == "fetch_end":
            self.fetch_finished.emit(payload)
        else:
            self.error.emit(str(payload))

    def connect_strava(self, client_id: str, client_secret: str) -> None:
        worker = self._worker
        worker.cancel_login.clear()
        worker.submit(lambda: worker.connect(client_id, client_secret))

    def cancel_login(self) -> None:
        self._worker.cancel_login.set()

    def disconnect_strava(self) -> None:
        self._worker.submit(self._worker.disconnect)

    def log_in(
        self, source: str, action: Callable[[ActivitySource], object], final: bool = True
    ) -> None:
        """Runs on the worker; answered with source_changed. final: the last step."""
        self._worker.submit(lambda: self._worker.account(source, action, True, final))

    def log_out(self, source: str) -> None:
        worker = self._worker
        worker.submit(lambda: worker.account(source, lambda c: c.disconnect(), False, True))

    def fetch(self, job: FetchJob) -> None:
        self._worker.submit(lambda: self._worker.fetch(job))

    def upload(self, job: UploadJob) -> None:
        self._worker.submit(lambda: self._worker.upload(job))

    def import_files(self, paths: list[Path]) -> None:
        worker = self._worker
        worker.cancel_import.clear()
        worker.submit(lambda: worker.import_files(paths))

    def cancel_import(self) -> None:
        self._worker.cancel_import.set()

    def acknowledge(self) -> None:
        self._worker.acknowledged.release()

    def set_riding(self, riding: bool) -> None:
        if riding:
            self._worker.may_run.clear()
        else:
            self._worker.may_run.set()

    def shutdown(self, timeout: float = 3.0) -> None:
        """Unfinished uploads stay stored as queued. Idempotent."""
        if self._on_destroyed is not None:
            self.destroyed.disconnect(self._on_destroyed)
            self._on_destroyed = None
        self._worker.close()
        if self._worker.join(timeout):
            self._gc.release()
        else:
            log.warning("sync thread did not end within %.1f s", timeout)
