"""Account and rides of one service on the sync page. Rules live in sync/service.py."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from pywatts.sync import garmin, wahoo
from pywatts.sync.service import SyncService
from pywatts.ui import theme
from pywatts.ui.widgets.common import text_label

FIELD_WIDTH = 360
AI_CONSENT = (
    "Rides fetched from {label} (power, heart rate, duration, load and their names) will then go "
    "to the provider of your AI program, e.g. Anthropic, whenever you use the coach. Its terms "
    "apply to them, and so do those of {label}. What was sent cannot be taken back; switching "
    "this off only stops it for the future.\n\nShow rides from {label} to the AI coach?"
)
WAHOO_STEPS = (
    "Log in and allow access in the browser. The page after that cannot be loaded, which is "
    "expected: copy its full address from the address bar and paste it here. If Wahoo reports an "
    "invalid scope (ungültiger Bereich), switch on user_read and workouts_read for your app in "
    "its developer portal."
)
GARMIN_NOTE = (
    "Experimental: not yet tried with a real account. Create a personal API key in Garmin Connect "
    "(valid for at most a year) and paste it here. Never give it to anybody else."
)


class SourceBox(QGroupBox):
    """Subclasses add the login."""

    label = ""

    def __init__(self, service: SyncService, name: str, parent: QWidget | None = None) -> None:
        super().__init__(self.label, parent)
        self._service = service
        self._name = name
        self._busy = False  # a login step is at the worker
        self._error = ""  # of the last step
        self.status = text_label("chip")
        self.status.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.connect_button = QPushButton("Connect")
        theme.set_role(self.connect_button, "primary")
        self.connect_button.clicked.connect(lambda: self._guard(self._log_in))
        self.disconnect_button = QPushButton("Disconnect")
        theme.set_role(self.disconnect_button, "danger")
        self.disconnect_button.clicked.connect(lambda: self._guard(self._disconnect))
        self.auto = QCheckBox("Fetch new rides after the start")
        self.auto.clicked.connect(
            lambda on: self._guard(lambda: self._service.set_auto_fetch(self._name, on))
        )
        self.ai = QCheckBox("Show these rides to the AI coach")
        self.ai.clicked.connect(lambda on: self._guard(lambda: self._share(on)))
        self.check_button = QPushButton("Check now")
        self.check_button.clicked.connect(lambda: self._guard(lambda: service.fetch(name)))
        self.all_button = QPushButton("Fetch everything")
        self.all_button.clicked.connect(
            lambda: self._guard(lambda: service.fetch(name, everything=True))
        )
        self.result = self._wrapped("")
        self.message = self._wrapped("")

        form = QFormLayout(self)
        form.addRow("Status", self.status)
        self._add_login(form)
        form.addRow("", self._row(self.connect_button, self.disconnect_button))
        form.addRow("", self.auto)
        form.addRow("", self.ai)
        form.addRow("", self._row(self.check_button, self.all_button))
        form.addRow("", self.result)
        form.addRow("", self.message)
        service.source_changed.connect(self._on_changed)
        service.login_finished.connect(self._on_login)
        self.refresh()

    # --- of the subclass ---

    def _add_login(self, form: QFormLayout) -> None:
        raise NotImplementedError

    def _log_in(self) -> None:
        raise NotImplementedError

    def _ready(self) -> bool:
        """Enough is filled in to log in."""
        raise NotImplementedError

    def _show_login(self, connected: bool) -> None:
        raise NotImplementedError

    def _logged_in(self, error: str) -> None:
        """A step of the login has ended."""
        raise NotImplementedError

    # --- shared ---

    @staticmethod
    def _wrapped(text: str) -> Any:
        label = text_label("muted", text)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _row(*buttons: QPushButton) -> QHBoxLayout:
        row = QHBoxLayout()
        for button in buttons:
            row.addWidget(button)
        row.addStretch(1)
        return row

    def _edit(self, secret: bool = False, text: str = "") -> QLineEdit:
        edit = QLineEdit(text)
        edit.setMaximumWidth(FIELD_WIDTH)
        if secret:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.textChanged.connect(lambda _t: self.refresh())
        return edit

    def _start_step(self, action: Callable[[Any], object], final: bool = True) -> None:
        self._busy, self._error = True, ""
        self._service.log_in(self._name, action, final)
        self.refresh()

    def _on_login(self, name: str, error: str) -> None:
        if name != self._name:
            return

        def show() -> None:
            self._busy, self._error = False, error
            self._logged_in(error)
            self._load()

        self._guard(show)

    def _on_changed(self, name: str) -> None:
        if name == self._name:
            self.refresh()

    def _disconnect(self) -> None:
        count = self._service.ride_count(self._name)
        delete = self.ask_delete(count) if count else False
        if delete is None:
            return
        self._error = ""
        self._reset()
        self._service.disconnect_source(self._name, delete)

    def _reset(self) -> None:
        """Forget a login that was begun."""

    def _share(self, on: bool) -> None:
        if not on or self.ask_ai():
            self._service.set_shared_with_ai(self._name, on)
        self._load()

    def refresh(self) -> None:
        self._guard(self._load)

    def _load(self) -> None:
        source = self._service.source(self._name)
        running = source.job is not None
        self.status.setText("Connected" if source.connected else "Not connected")
        theme.set_role(self.status, "ok" if source.connected else "", "tone")
        self.connect_button.setEnabled(not self._busy and self._ready())
        left = bool(source.error) or self._service.ride_count(self._name) > 0
        self.disconnect_button.setEnabled(not self._busy and (source.connected or left))
        self.auto.setChecked(self._service.auto_fetch(self._name))
        self.ai.setChecked(self._service.shared_with_ai(self._name))
        for button in (self.check_button, self.all_button):
            button.setEnabled(source.connected and not running)
        self.message.setText(self._error or source.error)
        self.result.setText(self._result())
        self._show_login(source.connected)

    def _result(self) -> str:
        source = self._service.source(self._name)
        s = source.summary
        if s is None:
            return ""
        text = f"{s.imported} new, {s.duplicates} already known, {s.skipped} other sports"
        if s.errors:
            text += f", {len(s.errors)} unreadable ({s.errors[-1]})"
        if source.job is not None:
            return f"Checking… {text}"
        if source.checked is not None:
            text = f"{source.checked.astimezone():%H:%M}: {text}"
        if source.result:
            text += f"\n{source.result}"
        if source.retry_at is not None:
            at = max(source.retry_at, datetime.now(UTC)).astimezone()
            text += f" Next try after {at:%H:%M}."
        return text

    def _guard(self, action: Callable[[], object]) -> None:
        """Never let a failure escape a slot: PyQt6 would abort the app."""
        try:
            action()
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            self.message.setText(self._error)

    # --- overridable dialogs ---

    def ask_delete(self, count: int) -> bool | None:
        """Delete the fetched rides too? None: cancelled."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(f"Disconnect {self.label}")
        what = "The ride" if count == 1 else f"All {count} rides"
        box.setText(
            f"{what} fetched from {self.label} can be deleted from pyWatts too. Deleting cannot "
            f"be undone, but after connecting again they can be fetched again."
        )
        if self._name == wahoo.NAME:
            box.setInformativeText("Wahoo's API terms require deletion when access is withdrawn.")
        delete = box.addButton("Delete rides", QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("Keep rides", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(delete)
        box.exec()
        clicked = box.clickedButton()
        return True if clicked is delete else False if clicked is keep else None

    def ask_ai(self) -> bool:
        answer = QMessageBox.question(self, "AI coach", AI_CONSENT.format(label=self.label))
        return answer == QMessageBox.StandardButton.Yes

    def open_url(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))


class WahooBox(SourceBox):
    label = "Wahoo"

    def __init__(self, service: SyncService, parent: QWidget | None = None) -> None:
        self._pasting = False  # the browser is open
        super().__init__(service, wahoo.NAME, parent)

    def _add_login(self, form: QFormLayout) -> None:
        self.client_id = self._edit()
        self.redirect = self._edit(text=wahoo.REDIRECT)
        self.steps = self._wrapped(WAHOO_STEPS)
        self.address = self._edit(secret=True)  # it contains the login code
        self.address.returnPressed.connect(self.connect_button.click)
        self.cancel_button = QPushButton("Cancel login")
        self.cancel_button.clicked.connect(lambda: self._guard(self._cancel))
        form.addRow("Client ID", self.client_id)
        form.addRow("Redirect", self.redirect)
        form.addRow("", self.steps)
        form.addRow("Address", self.address)
        form.addRow("", self._row(self.cancel_button))

    def _log_in(self) -> None:
        if self._pasting:
            address = self.address.text()
            self.address.clear()
            self._start_step(lambda client: client.finish_login(address))
        else:
            client_id, redirect = self.client_id.text(), self.redirect.text()
            self._start_step(lambda c: c.begin_login(client_id, redirect), final=False)

    def _cancel(self) -> None:
        self._reset()
        self._error = ""
        self._load()

    def _reset(self) -> None:
        self._pasting = False
        self.address.clear()

    def _ready(self) -> bool:
        if self._pasting:
            return bool(self.address.text().strip())
        return bool(self.client_id.text().strip() and self.redirect.text().strip())

    def _logged_in(self, error: str) -> None:
        if not error:
            self._pasting = not self._pasting

    def _show_login(self, connected: bool) -> None:
        for widget in (self.steps, self.address, self.cancel_button):
            widget.setVisible(self._pasting)
        form = self.layout()
        if isinstance(form, QFormLayout):
            for edit, shown in ((self.address, self._pasting), (self.redirect, not self._pasting)):
                form.setRowVisible(edit, shown)
        self.client_id.setEnabled(not self._pasting and not self._busy)
        self.redirect.setEnabled(not self._pasting and not self._busy)
        self.connect_button.setText("Finish login" if self._pasting else "Connect")
        self.cancel_button.setEnabled(not self._busy)


class GarminBox(SourceBox):
    label = "Garmin"

    def __init__(self, service: SyncService, parent: QWidget | None = None) -> None:
        super().__init__(service, garmin.NAME, parent)

    def _add_login(self, form: QFormLayout) -> None:
        self.key = self._edit(secret=True)
        self.key_button = QPushButton("Open Garmin Connect")
        self.key_button.clicked.connect(lambda: self._guard(lambda: self.open_url(garmin.KEY_PAGE)))
        form.addRow("", self._wrapped(GARMIN_NOTE))
        form.addRow("API key", self.key)
        form.addRow("", self._row(self.key_button))

    def _log_in(self) -> None:
        key = self.key.text()
        self._start_step(lambda client: client.connect(key))

    def _ready(self) -> bool:
        return bool(self.key.text().strip())

    def _logged_in(self, error: str) -> None:
        if not error:
            self.key.clear()

    def _show_login(self, connected: bool) -> None:
        self.key.setEnabled(not self._busy)

    def _reset(self) -> None:
        self.key.clear()
