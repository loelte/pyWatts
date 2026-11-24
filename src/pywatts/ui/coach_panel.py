"""AI coach panel: starts the AI program in a terminal and lists what it changed."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pywatts import platform
from pywatts.ai import launch
from pywatts.ai.pty import PtyBridge
from pywatts.storage.plan_repo import Change, PlanError, PlanRepo
from pywatts.storage.repo import COACH_COMMAND, COACH_CONSENT, COACH_PATH, Repo
from pywatts.ui import theme
from pywatts.ui.widgets.common import text_label
from pywatts.ui.widgets.terminal import TerminalWidget

log = logging.getLogger(__name__)

CHANGES = 50
CHANGES_HEIGHT = 220

PLACEHOLDER = (
    "The coach is an AI program that runs here. It reads your training data, and it creates and "
    "reviews plans and workouts.\n\n"
    f"1. Install Claude Code: {launch.INSTALL_URL}\n"
    "2. Run “claude” in a terminal of your system once and log in.\n"
    "3. Press Start. The first time, Claude Code asks about the folder. It is an empty one of "
    "pyWatts: choose “Yes, I trust this folder”.\n\n"
    "Another program can be set in Settings."
)
CONSENT = (
    "The AI program sends data to its provider: your training rides with power and heart-rate "
    "data, your name, age and weight, and other profile data such as FTP and zones. The "
    "terms of the provider apply to this data. If several riders use this device, it also sees "
    "the names and descriptions of all workouts in the library they share.\n\n"
    "Nothing starts by itself: the program only runs after you press Start. What it changes in "
    "your plans and workouts is listed under Changes and can be undone.\n\n"
    "Do you agree?"
)


class CoachPanel(QWidget):
    """coach: ai/service.CoachService. Nothing runs before the rider presses Start."""

    undone = pyqtSignal()

    def __init__(
        self,
        repo: Repo,
        plans: PlanRepo,
        coach: Any,
        parent: QWidget | None = None,
        bridge_factory: Callable[[], Any] = PtyBridge,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._plans = plans
        self._coach = coach
        self._running = False
        self._bridge = bridge_factory()
        if isinstance(self._bridge, QObject):
            self._bridge.setParent(self)
        self._build()
        self.terminal.attach(self._bridge)
        self._bridge.failed.connect(lambda text: self._guard(lambda: self._on_failed(text)))
        self._bridge.finished.connect(lambda code: self._guard(lambda: self._on_finished(code)))
        coach.plan_changed.connect(lambda: self._guard(self._refresh_changes))
        coach.workouts_changed.connect(lambda: self._guard(self._refresh_changes))
        coach.activity.connect(self.activity.setText)
        self._show_state("Stopped")

    def _build(self) -> None:
        self.status = text_label("chip")
        self.status.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.start_button = QPushButton()
        self.start_button.clicked.connect(lambda: self._guard(self._toggle))
        self.restart_button = QPushButton("Restart")
        self.restart_button.clicked.connect(lambda: self._guard(self.restart))
        self.changes_button = QPushButton("Changes")
        self.changes_button.setCheckable(True)
        self.changes_button.setToolTip("What the AI changed")
        self.changes_button.toggled.connect(lambda _on: self._guard(self._refresh_changes))

        self.message = text_label("error")
        self.message.setWordWrap(True)
        self.message.hide()

        self.changes = QScrollArea()
        self.changes.setWidgetResizable(True)
        self.changes.setFrameShape(QFrame.Shape.NoFrame)
        self.changes.setMaximumHeight(CHANGES_HEIGHT)
        self.changes.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.changes.hide()

        self.placeholder = text_label("muted", PLACEHOLDER)
        self.placeholder.setWordWrap(True)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.placeholder.setContentsMargins(4, 8, 4, 8)
        self.placeholder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.terminal = TerminalWidget()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.placeholder)
        self.stack.addWidget(self.terminal)
        self.stack.setMinimumHeight(120)

        self.activity = text_label("muted")
        self.activity.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        heading = QHBoxLayout()
        heading.addWidget(text_label("heading", "AI coach"))
        heading.addStretch(1)
        heading.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.restart_button)
        buttons.addStretch(1)
        buttons.addWidget(self.changes_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(heading)
        layout.addLayout(buttons)
        layout.addWidget(self.message)
        layout.addWidget(self.changes)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.activity)

    # --- program ---

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running or not self._consented():
            return
        self._show_error("")
        self._stop_program()  # what is left of a program that ended
        try:
            prepared = self._coach.prepare()
            argv = launch.build(
                prepared,
                self._repo.get_setting(COACH_COMMAND),
                self._repo.get_setting(COACH_PATH),
            )
            self.terminal.reset()
            self.stack.setCurrentWidget(self.terminal)
            self.terminal.apply_size()
            self._bridge.start(
                argv,
                platform.coach_dir(self._repo.rider_id),
                dict(prepared.env),
                *self.terminal.grid(),
            )
        except Exception:
            self._show_state("Stopped")
            self._stop_program()
            raise
        self._running = True
        self.activity.clear()
        self._show_state("Running")
        self.terminal.setFocus()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._show_state("Stopped")
        self._stop_program()

    def restart(self) -> None:
        self.stop()
        self.start()

    def shutdown(self) -> None:
        """Ends the program. Idempotent."""
        self._running = False
        self._guard(self._stop_program)
        self._show_state("Stopped")

    def _toggle(self) -> None:
        if self._running:
            self.stop()
        else:
            self.start()

    def _stop_program(self) -> None:
        try:
            self._bridge.stop()
        finally:
            self._coach.stop()

    def _consented(self) -> bool:
        if self._repo.get_setting(COACH_CONSENT) == "1":
            return True
        if not self.ask_consent():
            return False
        self._repo.set_setting(COACH_CONSENT, "1")
        return True

    def _on_failed(self, text: str) -> None:
        self._running = False
        self._show_state("Stopped")
        self._show_error(f"The AI program could not be started: {text}")

    def _on_finished(self, code: int | None) -> None:
        self._coach.stop()
        if self._running:  # not after failed
            self._running = False
            self._show_state("Exited" if code is None else f"Exited {code}", keep_terminal=True)

    # --- changes of the AI ---

    def _refresh_changes(self) -> None:
        shown = self.changes_button.isChecked()
        self.changes.setVisible(shown)
        self.changes_button.setText("Hide changes" if shown else "Changes")
        if not shown:
            return
        rows = QWidget()
        layout = QVBoxLayout(rows)
        layout.setContentsMargins(0, 0, 4, 0)
        changes = self._plans.changes(CHANGES, actor="ai")
        for change in changes:
            layout.addWidget(self._change_row(change))
        if not changes:
            layout.addWidget(text_label("muted", "The AI has changed nothing yet."))
        layout.addStretch(1)
        bar = self.changes.verticalScrollBar()
        position = bar.value() if bar is not None else 0
        old = self.changes.takeWidget()
        if old is not None:
            old.deleteLater()  # later: one of its buttons may be in its slot
        self.changes.setWidget(rows)
        if bar is not None:
            bar.setValue(position)

    def _change_row(self, change: Change) -> QWidget:
        row = QFrame()
        theme.set_role(row, "card")
        texts = QVBoxLayout()
        texts.setSpacing(0)
        summary = QLabel(change.summary)
        summary.setTextFormat(Qt.TextFormat.PlainText)
        summary.setWordWrap(True)
        summary.setMinimumWidth(1)
        texts.addWidget(summary)
        texts.addWidget(text_label("muted", f"{change.at_utc.astimezone():%Y-%m-%d %H:%M}"))
        button = QPushButton("Undone" if change.undone else "Undo")
        button.setEnabled(not change.undone)
        button.clicked.connect(lambda: self._guard(lambda: self._undo(change.batch)))
        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addLayout(texts, 1)
        layout.addWidget(button)
        return row

    def _undo(self, batch: str) -> None:
        self._show_error("")
        self._plans.undo(batch)
        self._refresh_changes()
        self.undone.emit()

    # --- overridable dialogs ---

    def ask_consent(self) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("AI coach")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(CONSENT)
        agree = box.addButton("Agree", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is agree

    # --- helpers ---

    def _show_state(self, text: str, keep_terminal: bool = False) -> None:
        self.status.setText(text)
        tone = "ok" if self._running else "warn" if keep_terminal else ""
        theme.set_role(self.status, tone, "tone")
        self.start_button.setText("Stop" if self._running else "Start")
        theme.set_role(self.start_button, "danger" if self._running else "primary")
        self.restart_button.setEnabled(self._running)
        if not self._running and not keep_terminal:
            self.stack.setCurrentWidget(self.placeholder)

    def _show_error(self, text: str) -> None:
        self.message.setText(text)
        self.message.setVisible(bool(text))

    def _guard(self, action: Callable[[], object]) -> None:
        """Never let a failure escape a slot: PyQt6 would abort the app."""
        try:
            action()
        except (PlanError, launch.LaunchError) as e:
            self._show_error(str(e))
        except Exception as e:
            log.exception("coach panel failed")
            self._show_error(f"{type(e).__name__}: {e}")
