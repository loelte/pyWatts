"""Rules for uploads and imports. GUI thread; the only sync part that uses the Repo."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from pywatts.storage.import_files import FileResult
from pywatts.storage.repo import STRAVA_AUTO, Repo, SessionRow, Upload, service_key
from pywatts.sync.thread import (
    FetchedRide,
    FetchEnd,
    FetchJob,
    SourceState,
    SyncBridge,
    UploadJob,
)

log = logging.getLogger(__name__)

MIN_AUTO_S = 60
RETRY_MS = 5 * 60 * 1000
TITLE_SUFFIX = "with pyWatts"
START_MS = 5000  # after the start, when the window is usable
FIRST_DAYS = 90  # fetched after connecting
OVERLAP_DAYS = 14  # looked at again, because devices upload late
_MAX_TITLE = 80


class RideSource(Protocol):
    """RideController."""

    session_finished: Any
    state_changed: Any

    @property
    def is_active(self) -> bool: ...


@dataclass
class ImportSummary:
    imported: int = 0
    duplicates: int = 0
    skipped: int = 0  # other sports and formats
    errors: list[str] = field(default_factory=list)


@dataclass
class Source:
    """A service rides are fetched from."""

    connected: bool = False
    error: str = ""  # of the account
    job: FetchJob | None = None  # running
    started: datetime | None = None  # of that job
    summary: ImportSummary | None = None  # of the running or last job
    result: str = ""  # of the last job, if it failed
    retry_at: datetime | None = None  # the last job is to be repeated
    checked: datetime | None = None  # last complete job since the start


def external_id(session: SessionRow) -> str:
    """Stable, so that a repeated upload is recognised."""
    return f"pywatts-{session.started_at_utc:%Y%m%dT%H%M%S%f}"


def activity_name(session: SessionRow) -> str:
    """Title on Strava; imported rides were not ridden with pyWatts."""
    name = session.workout.name.strip()
    if session.source != "pywatts":
        return name
    return f"{name or 'Ride'} {TITLE_SUFFIX}"


class SyncService(QObject):
    uploads_changed = pyqtSignal(int)  # session id
    connected_changed = pyqtSignal(bool)
    import_progress = pyqtSignal(object)  # ImportSummary
    import_finished = pyqtSignal(object)  # ImportSummary
    source_changed = pyqtSignal(str)
    login_finished = pyqtSignal(str, str)  # source, error: a step of a login
    fetch_finished = pyqtSignal(str, object)  # source, ImportSummary
    error = pyqtSignal(str)

    def __init__(
        self, repo: Repo, bridge: SyncBridge, rides: RideSource, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._bridge = bridge
        self._rides = rides
        self._active: set[int] = set()  # sessions at the worker
        self._import: ImportSummary | None = None
        self._sources: dict[str, Source] = {}
        self._waiting: set[str] = set()  # to be fetched when the start is over
        self._starter = QTimer(self, interval=START_MS, singleShot=True)
        self._starter.timeout.connect(self._fetch_waiting)
        rides.session_finished.connect(self._on_ride_finished)
        rides.state_changed.connect(lambda _s: self._bridge.set_riding(self._rides.is_active))
        bridge.connected_changed.connect(self._on_connected)
        bridge.upload_progress.connect(lambda u: self._guard(lambda: self._store(u)))
        bridge.upload_finished.connect(self._on_upload_finished)
        bridge.file_ready.connect(self._on_file)
        bridge.import_finished.connect(self._on_import_finished)
        bridge.source_changed.connect(lambda s: self._guard(lambda: self._on_source(s)))
        bridge.ride_fetched.connect(self._on_fetched)
        bridge.fetch_finished.connect(lambda e: self._guard(lambda: self._on_fetch_end(e)))
        bridge.error.connect(self.error)
        self._timer = QTimer(self, interval=RETRY_MS)
        self._timer.timeout.connect(self.resume_uploads)
        self._timer.timeout.connect(self.resume_fetches)
        self._timer.start()

    # --- Strava ---

    @property
    def connected(self) -> bool:
        return self._bridge.connected

    @property
    def auto_upload(self) -> bool:
        return self._repo.get_setting(STRAVA_AUTO) == "1"

    def set_auto_upload(self, on: bool) -> None:
        """Only affects rides finished from now on."""
        self._repo.set_setting(STRAVA_AUTO, "1" if on else "0")

    def connect_strava(self, client_id: str, client_secret: str) -> None:
        self._bridge.connect_strava(client_id, client_secret)

    def cancel_login(self) -> None:
        self._bridge.cancel_login()

    def disconnect_strava(self) -> None:
        self._bridge.disconnect_strava()

    def needs_confirmation(self, session: SessionRow) -> bool:
        """Imported rides are probably on Strava already: the UI asks first."""
        return session.source != "pywatts"

    def upload(self, session_id: int) -> None:
        """Queue a ride; also repeats a failed upload. Survives a restart."""
        self._guard(lambda: self._queue(session_id))

    def _queue(self, session_id: int) -> None:
        if not self.connected:
            self.error.emit("Connect to Strava first.")
            return
        known = self._repo.get_upload(session_id)
        if session_id in self._active or (known is not None and known.state == "done"):
            return
        session = self._repo.get_session(session_id)
        if session is None or session.ended_at_utc is None:
            return
        if known is None or known.state == "failed":
            known = Upload(session_id, "queued")
            self._store(known)
        self._submit(known, session)

    def _submit(self, upload: Upload, session: SessionRow) -> None:
        samples = self._repo.get_samples(session.id)
        self._bridge.upload(
            UploadJob(
                session, samples, activity_name(session), external_id(session), upload.upload_id
            )
        )
        self._active.add(session.id)

    def resume_uploads(self) -> None:
        """Queued rides from earlier; one at a time keeps the memory small."""
        self._guard(self._resume)

    def _resume(self) -> None:
        if not self.connected or self._active:
            return
        for upload in self._repo.list_uploads("queued", "processing"):
            session = self._repo.get_session(upload.session_id)
            if session is not None and session.ended_at_utc is not None:
                self._submit(upload, session)

    def _on_connected(self, connected: bool) -> None:
        self.connected_changed.emit(connected)
        if connected:
            self.resume_uploads()

    def _on_ride_finished(self, session_id: int) -> None:
        def check() -> None:
            session = self._repo.get_session(session_id)
            if (
                session is not None
                and session.status == "completed"
                and session.ended_at_utc is not None  # else saving it failed
                and session.source == "pywatts"
                and (session.duration_s or 0) >= MIN_AUTO_S
                and self.auto_upload
                and self.connected
            ):
                self._queue(session_id)

        self._guard(check)

    def _on_upload_finished(self, upload: Upload) -> None:
        self._active.discard(upload.session_id)
        self._guard(lambda: self._store(upload))
        if upload.error:
            self.error.emit(f"Strava upload: {upload.error}")

    def _store(self, upload: Upload) -> None:
        if self._repo.get_session(upload.session_id) is None:
            return  # deleted meanwhile
        self._repo.save_upload(upload)
        self.uploads_changed.emit(upload.session_id)

    # --- files ---

    @property
    def importing(self) -> bool:
        return self._import is not None

    def import_files(self, paths: list[Path]) -> bool:
        """Files, folders, zips. False while another import runs."""
        if self._import is not None:
            return False
        self._import = ImportSummary()
        self._bridge.import_files(paths)
        return True

    def cancel_import(self) -> None:
        self._bridge.cancel_import()

    def _on_file(self, result: FileResult) -> None:
        summary = self._import or ImportSummary()
        try:
            if result.ride is not None:
                outcome = self._repo.import_session(
                    result.ride, result.source, result.external_id, result.sha256
                )
                if outcome.result == "imported":
                    summary.imported += 1
                else:
                    summary.duplicates += 1
            elif result.skipped:
                summary.skipped += 1
            else:
                summary.errors.append(f"{result.name}: {result.error}")
        except Exception as e:
            log.exception("import failed")
            summary.errors.append(f"{result.name}: {e}")
        finally:
            self._bridge.acknowledge()
        self.import_progress.emit(replace(summary, errors=list(summary.errors)))

    def _on_import_finished(self) -> None:
        summary, self._import = self._import, None
        self.import_finished.emit(summary or ImportSummary())

    # --- rides of services ---

    def source(self, name: str) -> Source:
        return self._sources.setdefault(name, Source())

    def log_in(self, name: str, action: Callable[[Any], object], final: bool = True) -> None:
        """A step of a login, the last one if final. action gets the client and runs on the
        worker thread; login_finished answers."""
        self._bridge.log_in(name, action, final)

    def disconnect_source(self, name: str, delete_rides: bool) -> None:
        self._guard(lambda: self._disconnect(name, delete_rides))

    def _disconnect(self, name: str, delete_rides: bool) -> None:
        source = self.source(name)
        if source.job is not None:
            source.job.cancel.set()  # what it still delivers is ignored
        self._waiting.discard(name)
        source.retry_at, source.summary, source.result, source.checked = None, None, "", None
        self._bridge.log_out(name)
        for what in ("since", "checked", "full", "ai"):
            self._repo.set_setting(service_key(name, what), "")
        if delete_rides and self._repo.forget_service(name):
            self.fetch_finished.emit(name, ImportSummary())
        self.source_changed.emit(name)

    def ride_count(self, name: str) -> int:
        return len(self._repo.service_sessions(name))

    def auto_fetch(self, name: str) -> bool:
        return self._repo.get_setting(service_key(name, "auto")) != "0"

    def set_auto_fetch(self, name: str, on: bool) -> None:
        self._repo.set_setting(service_key(name, "auto"), "" if on else "0")

    def shared_with_ai(self, name: str) -> bool:
        return self._repo.get_setting(service_key(name, "ai")) == "1"

    def set_shared_with_ai(self, name: str, on: bool) -> None:
        self._repo.set_setting(service_key(name, "ai"), "1" if on else "")

    def fetch(self, name: str, everything: bool = False) -> None:
        """Look for new rides now. everything: survives a restart until it is complete."""
        self._guard(lambda: self._fetch(name, everything))

    def _fetch(self, name: str, everything: bool = False) -> None:
        source = self.source(name)
        if not source.connected:
            return
        if everything:
            self._repo.set_setting(service_key(name, "full"), "1")
            if source.job is not None:
                source.retry_at = datetime.now(UTC)  # once this one has ended
        if source.job is not None:
            return
        now = datetime.now(UTC)
        job = FetchJob(
            name, frozenset(self._repo.known_imports(name)), self._fetch_since(name, now)
        )
        source.job, source.started, source.retry_at = job, now, None
        source.summary, source.result = ImportSummary(), ""
        self._bridge.fetch(job)
        self.source_changed.emit(name)

    def _fetch_since(self, name: str, now: datetime) -> datetime | None:
        if self._repo.get_setting(service_key(name, "full")) == "1":
            return None
        since = _time(self._repo.get_setting(service_key(name, "since")))
        if since is None:
            since = now - timedelta(days=FIRST_DAYS)
            self._repo.set_setting(service_key(name, "since"), since.isoformat())
        checked = _time(self._repo.get_setting(service_key(name, "checked")))
        return since if checked is None else max(since, checked - timedelta(days=OVERLAP_DAYS))

    def resume_fetches(self) -> None:
        """Those that failed and can be repeated."""
        now = datetime.now(UTC)
        for name, source in list(self._sources.items()):
            due = source.retry_at is not None and source.retry_at <= now
            if due and not self._rides.is_active:
                self.fetch(name)

    def _fetch_waiting(self) -> None:
        names, self._waiting = self._waiting, set()
        for name in names:
            self.fetch(name)

    def _on_source(self, state: SourceState) -> None:
        source = self.source(state.source)
        appeared = state.connected and not source.connected
        source.connected = state.connected
        source.error = "" if state.login else state.error  # else login_finished tells
        if not state.connected:
            source.retry_at = None
        full = self._repo.get_setting(service_key(state.source, "full")) == "1"
        if appeared and (full or self.auto_fetch(state.source)):
            self._waiting.add(state.source)
            self._starter.start()
        self.source_changed.emit(state.source)
        if state.login:
            self.login_finished.emit(state.source, state.error)

    def _on_fetched(self, fetched: FetchedRide) -> None:
        name, ride, result = fetched.job.source, fetched.ride, fetched.result
        summary = self.source(name).summary or ImportSummary()
        try:
            if fetched.job.cancel.is_set():
                pass  # disconnected meanwhile
            elif result.ride is not None:
                title = " ".join(ride.title.split())[:_MAX_TITLE].strip()
                found = replace(result.ride, title=title or result.ride.title)
                outcome = self._repo.import_session(found, name, ride.external_id, result.sha256)
                if outcome.result == "imported":
                    summary.imported += 1
                else:
                    summary.duplicates += 1
            else:  # never asked for again
                self._repo.skip_import(name, ride.external_id, result.sha256 or None)
                if result.skipped:
                    summary.skipped += 1
                else:
                    summary.errors.append(f"{result.name}: {result.error}")
        except Exception as e:
            log.exception("storing a ride failed")
            summary.errors.append(f"{result.name}: {e}")
        finally:
            self._bridge.acknowledge()
        self.source_changed.emit(name)

    def _on_fetch_end(self, end: FetchEnd) -> None:
        name = end.job.source
        source = self.source(name)
        if source.job is not end.job:
            return
        started, again = source.started, source.retry_at
        source.job = None
        if end.job.cancel.is_set():
            self.source_changed.emit(name)
            return
        source.result = end.error
        now = datetime.now(UTC)
        if end.complete and started is not None:
            source.checked = now
            self._repo.set_setting(service_key(name, "checked"), started.isoformat())
            if end.job.since is None:
                self._repo.set_setting(service_key(name, "full"), "")
        elif end.retry_after_s is not None:
            source.retry_at = now + timedelta(seconds=end.retry_after_s)
        summary = source.summary or ImportSummary()
        if summary.imported:
            self.fetch_finished.emit(name, replace(summary, errors=list(summary.errors)))
        self.source_changed.emit(name)
        if again is not None and end.complete:  # everything was asked for meanwhile
            self._fetch(name)

    def _guard(self, action: Callable[[], None]) -> None:
        """Never let a failure escape a slot: PyQt6 would abort the app."""
        try:
            action()
        except Exception as e:
            log.exception("sync failure")
            self.error.emit(f"{type(e).__name__}: {e}")


def _time(text: str) -> datetime | None:
    try:
        found = datetime.fromisoformat(text)
    except ValueError:
        return None
    return found if found.tzinfo is not None else None
