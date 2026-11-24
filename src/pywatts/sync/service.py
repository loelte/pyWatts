"""Rules for uploads and imports. GUI thread; the only sync part that uses the Repo."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from pywatts.storage.import_files import FileResult
from pywatts.storage.repo import STRAVA_AUTO, Repo, SessionRow, Upload
from pywatts.sync.thread import SyncBridge, UploadJob

log = logging.getLogger(__name__)

MIN_AUTO_S = 60
RETRY_MS = 5 * 60 * 1000
TITLE_SUFFIX = "with pyWatts"


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
        rides.session_finished.connect(self._on_ride_finished)
        rides.state_changed.connect(lambda _s: self._bridge.set_riding(self._rides.is_active))
        bridge.connected_changed.connect(self._on_connected)
        bridge.upload_progress.connect(lambda u: self._guard(lambda: self._store(u)))
        bridge.upload_finished.connect(self._on_upload_finished)
        bridge.file_ready.connect(self._on_file)
        bridge.import_finished.connect(self._on_import_finished)
        bridge.error.connect(self.error)
        self._timer = QTimer(self, interval=RETRY_MS)
        self._timer.timeout.connect(self.resume_uploads)
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

    def _guard(self, action: Callable[[], None]) -> None:
        """Never let a failure escape a slot: PyQt6 would abort the app."""
        try:
            action()
        except Exception as e:
            log.exception("sync failure")
            self.error.emit(f"{type(e).__name__}: {e}")
