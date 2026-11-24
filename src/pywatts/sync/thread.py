"""Sync worker thread + Qt bridge. No database access here: results go to the GUI thread."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from queue import Queue

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from pywatts.storage.fit_export import fit_bytes
from pywatts.storage.import_files import read_files
from pywatts.storage.repo import Sample, SessionRow, Upload
from pywatts.sync.base import NotConnected, RateLimited, SyncError, Transient
from pywatts.sync.gui_gc import GuiGc
from pywatts.sync.strava import StravaClient, UploadStatus

log = logging.getLogger(__name__)

RETRY_WAITS_S = (2.0, 8.0)
POLL_S = 2.0
POLL_COUNT = 30


@dataclass(frozen=True)
class UploadJob:
    session: SessionRow
    samples: list[Sample]
    name: str
    external_id: str
    upload_id: str | None = None  # already accepted, only poll


class _Stopped(Exception):
    pass


class _Worker:
    """No Qt. Hands results to `post`, which is never called once closed."""

    def __init__(
        self,
        factory: Callable[[], StravaClient] | None,
        post: Callable[[str, object], object],
    ) -> None:
        self._factory = factory
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

    def import_files(self, paths: list[Path]) -> None:
        try:
            results = read_files(paths)
            while True:
                self._gate()  # before the next file is decoded
                if self.cancel_import.is_set() or (result := next(results, None)) is None:
                    break
                self.post("file", result)
                # One at a time, so that decoded rides do not pile up in memory.
                while not self.acknowledged.acquire(timeout=0.2):
                    if self.stop.is_set():
                        raise _Stopped
        finally:
            self.post("import_finished", None)

    def upload(self, job: UploadJob) -> None:
        sid = job.session.id
        upload_id = job.upload_id
        try:
            self._gate()
            if time.monotonic() < self._blocked_until:
                raise Transient("Waiting for Strava's request limit.")
            client = self._need_client()
            if upload_id is None:
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
    Every file_ready must be answered with acknowledge().
    """

    connected_changed = pyqtSignal(bool)
    upload_progress = pyqtSignal(object)  # Upload
    upload_finished = pyqtSignal(object)  # Upload
    file_ready = pyqtSignal(object)  # FileResult
    import_finished = pyqtSignal()
    error = pyqtSignal(str)

    _posted = pyqtSignal(str, object)

    def __init__(
        self, factory: Callable[[], StravaClient] | None, parent: QObject | None = None
    ) -> None:
        """factory runs on the worker thread; None = no network."""
        super().__init__(parent)
        self.connected = False
        self._gc = GuiGc(self)  # before the worker starts
        self._posted.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)
        worker = self._worker = _Worker(factory, self._posted.emit)
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
