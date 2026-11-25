"""Sync page: accounts and ride import. Rules live in sync/service.py."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from pywatts import platform
from pywatts.sync.service import ImportSummary, SyncService
from pywatts.ui import theme
from pywatts.ui.widgets.common import GAP, page_layout, text_label
from pywatts.ui.widgets.source_box import GarminBox, WahooBox

IMPORT_NOTE = (
    "Cycling activities from .fit files, folders or .zip archives. Rides already in the history "
    "are skipped. Training load uses the FTP logged for the day of the ride, so log older FTP "
    "values in Progress first."
)

RIDER_NOTE = (
    "Fetched and imported rides go into the history of {name}, uploads to the Strava account "
    "connected here. Before connecting, log out of strava.com, wahooligan.com or Garmin Connect "
    "in the browser if another rider is logged in there."
)
PRIVACY_URL = "https://github.com/loelte/pyWatts/blob/HEAD/PRIVACY.md"


class SyncPage(QWidget):
    def __init__(self, service: SyncService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._logging_in = False
        self._build()
        service.connected_changed.connect(self._on_connected)
        service.error.connect(self.message.setText)
        service.import_progress.connect(self._on_progress)
        service.import_finished.connect(self._on_import_finished)
        self._update()

    def _build(self) -> None:
        self.status = text_label("chip")
        self.status.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.client_id = QLineEdit()
        self.client_secret = QLineEdit()
        self.client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_id.textChanged.connect(lambda _t: self._update())
        self.client_secret.textChanged.connect(lambda _t: self._update())
        self.connect_button = QPushButton()
        theme.set_role(self.connect_button, "primary")
        self.connect_button.clicked.connect(lambda: self._guard(self._toggle_login))
        self.disconnect_button = QPushButton("Disconnect")
        theme.set_role(self.disconnect_button, "danger")
        self.disconnect_button.clicked.connect(lambda: self._guard(self._service.disconnect_strava))
        self.auto = QCheckBox("Upload completed rides automatically")
        self.auto.clicked.connect(lambda on: self._guard(lambda: self._service.set_auto_upload(on)))

        strava = QGroupBox("Strava")
        form = QFormLayout(strava)
        self.client_id.setMaximumWidth(360)
        self.client_secret.setMaximumWidth(360)
        form.addRow("Status", self.status)
        form.addRow("Client ID", self.client_id)
        form.addRow("Client secret", self.client_secret)
        row = QHBoxLayout()
        row.addWidget(self.connect_button)
        row.addWidget(self.disconnect_button)
        row.addStretch(1)
        form.addRow("", row)
        form.addRow("", self.auto)

        self.files_button = QPushButton("Choose files…")
        self.files_button.clicked.connect(lambda: self._import(self.choose_files))
        self.folder_button = QPushButton("Choose folder…")
        self.folder_button.clicked.connect(lambda: self._import(self.choose_folder))
        self.zwift_button = QPushButton("Zwift folder")
        self.zwift_button.clicked.connect(lambda: self._import(platform.zwift_activity_dirs))
        self.zwift_button.setVisible(bool(platform.zwift_activity_dirs()))
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(lambda: self._guard(self._service.cancel_import))
        self.progress = text_label("muted")
        self.results = QListWidget()

        imports = QGroupBox("Import rides")
        note = text_label("muted", IMPORT_NOTE)
        note.setWordWrap(True)
        buttons = QHBoxLayout()
        for button in (
            self.files_button,
            self.folder_button,
            self.zwift_button,
            self.cancel_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        import_layout = QVBoxLayout(imports)
        import_layout.addWidget(note)
        import_layout.addLayout(buttons)
        import_layout.addWidget(self.progress)
        import_layout.addWidget(self.results, 1)

        self.message = text_label("muted")
        self.message.setWordWrap(True)
        self.rider = text_label("muted")
        self.rider.setWordWrap(True)
        self.rider.hide()
        self.wahoo = WahooBox(self._service)
        self.garmin = GarminBox(self._service)
        services = QHBoxLayout()
        services.setSpacing(GAP)
        for box in (self.wahoo, self.garmin):
            services.addWidget(box, 1)
        self.privacy_button = QPushButton("Privacy")
        self.privacy_button.clicked.connect(
            lambda: self._guard(lambda: self.wahoo.open_url(PRIVACY_URL))
        )
        head = QHBoxLayout()
        head.addWidget(text_label("title", "Sync"))
        head.addStretch(1)
        head.addWidget(self.privacy_button)
        self.results.setFixedHeight(120)
        content = QWidget()
        sections = QVBoxLayout(content)
        sections.setContentsMargins(0, 0, 0, 0)
        sections.setSpacing(GAP)
        sections.addWidget(strava)
        sections.addLayout(services)
        sections.addWidget(imports, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        page_layout(self, layout)
        layout.addLayout(head)
        layout.addWidget(self.rider)
        layout.addWidget(scroll, 1)
        # below the scroll area: errors of every section must be seen
        layout.addWidget(self.message)

    def set_rider(self, name: str) -> None:
        """Whose account and history this is; "" = there is only one rider."""
        self.rider.setText(RIDER_NOTE.format(name=name))
        self.rider.setVisible(bool(name))

    # --- Strava ---

    def _toggle_login(self) -> None:
        if self._logging_in:
            self._service.cancel_login()
            return
        self._service.connect_strava(self.client_id.text(), self.client_secret.text())
        self._logging_in = True
        self.message.setText("Allow access in the browser.")
        self._update()

    def _on_connected(self, connected: bool) -> None:
        """Also ends every login attempt."""
        if self._logging_in and connected:
            self.message.clear()
            self.client_secret.clear()
        self._logging_in = False
        self._update()

    # --- import ---

    def _import(self, choose: Callable[[], list[Path]]) -> None:
        def start() -> None:
            paths = choose()
            if paths and self._service.import_files(paths):
                self.results.clear()
                self.progress.setText("Reading…")
            self._update()

        self._guard(start)

    def _on_progress(self, summary: ImportSummary) -> None:
        self.progress.setText(
            f"{summary.imported} imported, {summary.duplicates} already known, "
            f"{summary.skipped} skipped, {len(summary.errors)} errors"
        )
        self.results.addItems(summary.errors[self.results.count() :])

    def _on_import_finished(self, summary: ImportSummary) -> None:
        self._on_progress(summary)
        self.progress.setText(f"Finished: {self.progress.text()}")
        self._update()

    # --- overridable dialogs ---

    def choose_files(self) -> list[Path]:
        names, _ = QFileDialog.getOpenFileNames(
            self, "Import rides", str(Path.home()), "Rides (*.fit *.fit.gz *.zip);;All files (*)"
        )
        return [Path(n) for n in names]

    def choose_folder(self) -> list[Path]:
        name = QFileDialog.getExistingDirectory(self, "Import rides", str(Path.home()))
        return [Path(name)] if name else []

    # --- helpers ---

    def _update(self) -> None:
        self._guard(self._load)

    def _load(self) -> None:
        connected, importing = self._service.connected, self._service.importing
        self.status.setText("Connected" if connected else "Not connected")
        theme.set_role(self.status, "ok" if connected else "", "tone")
        self.connect_button.setText("Cancel login" if self._logging_in else "Connect")
        filled = bool(self.client_id.text().strip() and self.client_secret.text().strip())
        self.connect_button.setEnabled(self._logging_in or filled)
        self.disconnect_button.setEnabled(connected and not self._logging_in)
        for edit in (self.client_id, self.client_secret):
            edit.setEnabled(not self._logging_in)
        self.auto.setChecked(self._service.auto_upload)
        for button in (self.files_button, self.folder_button, self.zwift_button):
            button.setEnabled(not importing)
        self.cancel_button.setEnabled(importing)

    def _guard(self, action: Callable[[], object]) -> None:
        """Never let a failure escape a slot: PyQt6 would abort the app."""
        try:
            action()
        except Exception as e:
            self.message.setText(f"{type(e).__name__}: {e}")
