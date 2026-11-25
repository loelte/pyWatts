"""Past rides: list, detail chart, delete, FIT export, Strava upload."""

from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QShowEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pywatts.core.metrics import average
from pywatts.storage.repo import SERVICES, Repo, Sample, SessionRow
from pywatts.sync.service import SyncService
from pywatts.sync.strava import activity_url
from pywatts.ui import theme
from pywatts.ui.ride import NO_VALUE, mmss
from pywatts.ui.widgets.common import GAP, card, page_layout, text_label
from pywatts.ui.widgets.ride_chart import RideChart
from pywatts.ui.widgets.ride_peaks import RidePeaksPanel

COLUMNS = ["Date", "Workout", "Duration", "Avg W", "kJ", "Status"]
_NUMERIC = (2, 3, 4)
_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_RESERVED = re.compile(r"(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?", re.IGNORECASE)
_MAX_NAME_BYTES = 120


def sanitize_filename(name: str) -> str:
    """File name (no extension) valid on Windows, macOS and Linux."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name)
    name = name.encode("utf-8", "ignore")[:_MAX_NAME_BYTES].decode("utf-8", "ignore")
    name = name.strip(" .")
    if _RESERVED.fullmatch(name):
        name = f"_{name}"
    return name or "ride"


def suggested_filename(session: SessionRow) -> str:
    local = session.started_at_utc.astimezone()
    return sanitize_filename(f"{local:%Y-%m-%d_%H%M}_{session.workout.name}") + ".fit"


def _num(value: float | None, unit: str = "") -> str:
    return NO_VALUE if value is None else f"{value:.0f}{unit}"


class HistoryPage(QWidget):
    def __init__(
        self, repo: Repo, sync: SyncService | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._sync = sync
        self._sessions: list[SessionRow] = []
        self._shown_id: int | None = None

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        vertical = self.table.verticalHeader()
        assert vertical is not None
        vertical.hide()
        vertical.setDefaultSectionSize(34)
        self.table.setShowGrid(False)
        for col in range(len(COLUMNS)):
            align = _RIGHT if col in _NUMERIC else _LEFT
            self.table.horizontalHeaderItem(col).setTextAlignment(align)

        self.summary_label = text_label("muted")
        self.summary_label.setWordWrap(True)
        self.chart = RideChart()
        self.power_curve = self.chart.curves["power"]
        self.peaks = RidePeaksPanel()
        self.strava_button = QPushButton("Upload to Strava")
        self.export_button = QPushButton("Export .fit")
        self.delete_button = QPushButton("Delete")
        theme.set_role(self.delete_button, "danger")
        buttons = QHBoxLayout()
        buttons.addWidget(self.summary_label, 1)
        buttons.addWidget(self.strava_button)
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.delete_button)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 4, 0, 0)
        detail_layout.setSpacing(GAP)
        detail_layout.addLayout(buttons)
        charts = QSplitter(Qt.Orientation.Horizontal)
        charts.addWidget(card(self.chart))
        charts.addWidget(card(self.peaks))
        charts.setChildrenCollapsible(False)
        charts.setStretchFactor(0, 3)
        charts.setStretchFactor(1, 1)
        charts.setSizes([720, 300])
        detail_layout.addWidget(charts, 1)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(detail)
        splitter.setChildrenCollapsible(False)
        layout = QVBoxLayout(self)
        page_layout(self, layout)
        layout.addWidget(text_label("title", "History"))
        layout.addWidget(splitter, 1)

        self.table.itemSelectionChanged.connect(self._show_selected)
        self.strava_button.clicked.connect(lambda: self.upload_selected())
        self.export_button.clicked.connect(lambda: self.export_selected())
        self.delete_button.clicked.connect(lambda: self.delete_selected())
        self.strava_button.setVisible(sync is not None)
        if sync is not None:
            sync.uploads_changed.connect(lambda _id: self._update_strava())
            sync.connected_changed.connect(lambda _on: self._update_strava())
        self.refresh()

    # --- public ---
    def refresh(self) -> None:
        try:
            rows = self._repo.list_sessions()
        except Exception as exc:
            self.show_error(f"Could not load the history: {exc}")
            return
        # No end time means the ride is still being recorded.
        self._sessions = [s for s in rows if s.ended_at_utc is not None]
        selected = self._shown_id
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setRowCount(len(self._sessions))
        for row, session in enumerate(self._sessions):
            cells = [
                f"{session.started_at_utc.astimezone():%Y-%m-%d %H:%M}",
                session.workout.name,
                NO_VALUE if session.duration_s is None else mmss(session.duration_s),
                _num(session.avg_power),
                _num(session.kj),
                session.status,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(_RIGHT if col in _NUMERIC else _LEFT)
                self.table.setItem(row, col, item)
        ids = [s.id for s in self._sessions]
        if selected in ids:
            self.table.selectRow(ids.index(selected))
        self.table.blockSignals(False)
        self._show_selected()

    def selected_session(self) -> SessionRow | None:
        model = self.table.selectionModel()
        rows = model.selectedRows() if model else []
        return self._sessions[rows[0].row()] if rows else None

    def delete_selected(self) -> None:
        session = self.selected_session()
        if session is None or not self.confirm_delete(session):
            return
        try:
            self._repo.delete_session(session.id)
        except Exception as exc:
            self.show_error(f"Could not delete the ride: {exc}")
        self.refresh()

    def export_selected(self) -> None:
        session = self.selected_session()
        if session is None:
            return
        path = self.choose_export_path(suggested_filename(session))
        if path is None:
            return
        try:
            from pywatts.storage.fit_export import export_fit  # slow to load

            export_fit(session, self._repo.get_samples(session.id), path)
        except Exception as exc:
            self.show_error(f"Export failed: {exc}")

    def upload_selected(self) -> None:
        """Upload, or open the activity if it is on Strava already."""
        session = self.selected_session()
        if session is None or self._sync is None:
            return
        try:
            upload = self._repo.get_upload(session.id)
            if upload is not None and upload.activity_id:
                self.open_url(activity_url(upload.activity_id))
            elif not self._sync.needs_confirmation(session) or self.confirm_upload(session):
                self._sync.upload(session.id)
        except Exception as exc:
            self.show_error(f"Upload failed: {exc}")
        self._update_strava()

    def _update_strava(self) -> None:
        session = self.selected_session()
        text, tip, enabled = "Upload to Strava", "", False
        if session is not None and self._sync is not None:
            try:
                upload = self._repo.get_upload(session.id)
            except Exception as exc:  # no dialog: this also runs in the background
                self.strava_button.setToolTip(str(exc))
                self.strava_button.setEnabled(False)
                return
            enabled = self._sync.connected
            tip = "" if enabled else "Connect to Strava on the Sync page first."
            if upload is not None and upload.activity_id:
                text, tip, enabled = "View on Strava", "", True
            elif upload is not None and upload.state == "failed":
                text, tip = "Retry upload", tip or upload.error or ""
            elif upload is not None:
                text, tip, enabled = "Uploading…", upload.error or "", False
        self.strava_button.setText(text)
        self.strava_button.setToolTip(tip)
        self.strava_button.setEnabled(enabled)

    # --- overridable dialogs ---
    def confirm_upload(self, session: SessionRow) -> bool:
        answer = QMessageBox.question(
            self,
            "Upload ride",
            "This ride was imported and may be on Strava already. Upload it anyway?",
        )
        return answer == QMessageBox.StandardButton.Yes

    def open_url(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    def confirm_delete(self, session: SessionRow) -> bool:
        answer = QMessageBox.question(
            self, "Delete ride", "Delete the selected ride? This cannot be undone."
        )
        return answer == QMessageBox.StandardButton.Yes

    def choose_export_path(self, suggested: str) -> Path | None:
        dialog = QFileDialog(self, "Export ride", str(Path.home()), "FIT files (*.fit)")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setDefaultSuffix("fit")
        dialog.selectFile(suggested)
        accepted = dialog.exec() == QFileDialog.DialogCode.Accepted
        files = dialog.selectedFiles()
        dialog.deleteLater()
        return Path(files[0]) if accepted and files else None

    def show_error(self, text: str) -> None:
        QMessageBox.warning(self, "History", text)

    # --- detail ---
    def showEvent(self, event: QShowEvent | None) -> None:
        super().showEvent(event)
        self.refresh()

    def _show_selected(self) -> None:
        session = self.selected_session()
        self.export_button.setEnabled(session is not None)
        self.delete_button.setEnabled(session is not None)
        self._update_strava()
        if session is None:
            self._shown_id = None
            self.summary_label.setText("Select a ride to see its details.")
            self._plot([])
            return
        self._shown_id = session.id
        try:
            samples = self._repo.get_samples(session.id)
        except Exception as exc:
            self.summary_label.setText("")
            self._plot([])
            self.show_error(f"Could not load the ride: {exc}")
            return
        power = [s.power for s in samples if s.power is not None]
        hr = [s.hr for s in samples if s.hr is not None]
        stats = [
            session.workout.name,
            f"FTP {session.ftp_w} W",
            f"NP {_num(session.np, ' W')}",
            f"Load {_num(session.load)}",
            f"Avg {_num(average(power) if power else None, ' W')}",
            f"Max {_num(max(power, default=None), ' W')}",
            f"Avg HR {_num(average(hr) if hr else None, ' bpm')}",
            f"Max HR {_num(max(hr, default=None), ' bpm')}",
        ]
        if session.source in SERVICES:
            stats.append(f"Data from {session.source.capitalize()}")
        self.summary_label.setText("   ·   ".join(stats))
        self._plot(samples, has_target=session.source == "pywatts", session=session)

    def _plot(
        self, samples: list[Sample], has_target: bool = True, session: SessionRow | None = None
    ) -> None:
        self.chart.set_samples(samples, has_target)
        ride: dict[int, float] = {}
        best = {}
        if session is not None:
            try:
                ride = self._repo.session_peaks(session.id)
                best = self._repo.best_peaks()
            except Exception as exc:
                self.show_error(f"Could not load the best power: {exc}")
        self.peaks.set_peaks(ride, best, session.weight_kg if session else None)
