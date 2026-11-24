"""Main window: sidebar + pages. Navigation only."""

from __future__ import annotations

import logging
from datetime import date
from uuid import UUID

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pywatts.ai.service import CoachService
from pywatts.core.workout import Workout
from pywatts.storage.plan_repo import PlanRepo
from pywatts.storage.repo import NAV_HIDDEN, Repo
from pywatts.sync.service import SyncService
from pywatts.trainer.thread import TrainerBridge
from pywatts.ui import theme
from pywatts.ui.coach_panel import CoachPanel
from pywatts.ui.editor import EditorPage
from pywatts.ui.history import HistoryPage
from pywatts.ui.plan import PlanPage
from pywatts.ui.profile import ProfilePage
from pywatts.ui.progress import ProgressPage
from pywatts.ui.ride import RidePage
from pywatts.ui.ride_controller import RideController
from pywatts.ui.settings import SettingsPage
from pywatts.ui.sync import SyncPage

LIBRARY, RIDE, PLAN, HISTORY, PROGRESS, PROFILE, SETTINGS, SYNC = range(8)
_FIRST_DAY = date(2000, 1, 1)

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(
        self,
        repo: Repo,
        bridge: TrainerBridge,
        controller: RideController,
        sync: SyncService | None = None,
        coach: CoachService | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("pyWatts")
        self.resize(1240, 800)
        self.controller = controller
        self.editor = EditorPage(repo)
        self.ride = RidePage(repo, bridge, controller)
        self._plans = PlanRepo(repo.connection)
        self.coach_panel = None if coach is None else CoachPanel(repo, self._plans, coach)
        self.plan = PlanPage(repo, self._plans, self.coach_panel)
        self.history = HistoryPage(repo, sync)
        self.progress = ProgressPage(repo)
        self.profile = ProfilePage(repo)
        self.settings = SettingsPage(repo, bridge)

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setIconSize(QSize(20, 20))
        for name in (
            "Library",
            "Ride",
            "Training Plan",
            "History",
            "Progress",
            "Profile",
            "Settings",
        ):
            self.sidebar.addItem(QListWidgetItem(theme.nav_icon(name.lower()), name))
        brand = QLabel("pyWatts")
        brand.setObjectName("brand")
        nav = QFrame()
        nav.setObjectName("nav")
        nav.setFixedWidth(168)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(12, 18, 12, 12)
        nav_layout.setSpacing(0)
        nav_layout.addWidget(brand)
        nav_layout.addWidget(self.sidebar, 1)
        self.stack = QStackedWidget()
        for page in (
            self.editor,
            self.ride,
            self.plan,
            self.history,
            self.progress,
            self.profile,
            self.settings,
        ):
            self.stack.addWidget(page)

        if sync is not None:
            self.sync = SyncPage(sync)
            self.stack.addWidget(self.sync)
            self.sidebar.addItem(QListWidgetItem(theme.nav_icon("sync"), "Sync"))
            sync.import_finished.connect(lambda _s: self._rides_changed(None))

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.nav_toggle = QToolButton()
        self.nav_toggle.setObjectName("navToggle")
        self.nav_toggle.setAutoRaise(True)
        self.nav_toggle.setFixedWidth(14)
        self.nav_toggle.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.nav_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.nav_toggle.clicked.connect(self.toggle_nav)
        layout.addWidget(nav)
        layout.addWidget(self.nav_toggle)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.sidebar.setCurrentRow(LIBRARY)
        self.editor.ride_requested.connect(self._ride)
        self.settings.settings_changed.connect(self.editor.refresh)
        self.profile.profile_changed.connect(self.editor.refresh)
        self.progress.metrics_changed.connect(self.editor.refresh)
        controller.session_finished.connect(self._rides_changed)
        self.plan.ride_requested.connect(self._ride)
        self.plan.workout_requested.connect(self._open_workout)
        if coach is not None and self.coach_panel is not None:
            coach.plan_changed.connect(self.plan.refresh)
            coach.workouts_changed.connect(self.editor.refresh)
            self.coach_panel.undone.connect(self.plan.refresh)
            self.coach_panel.undone.connect(self.editor.refresh)

        self._repo = repo
        self.nav = nav
        self.settings.settings_changed.connect(self._apply_nav)
        QShortcut(QKeySequence("Ctrl+B"), self, activated=self.toggle_nav)
        for row in range(self.sidebar.count()):  # reachable while the sidebar is hidden
            QShortcut(
                QKeySequence(f"Ctrl+{row + 1}"),
                self,
                activated=lambda row=row: self.sidebar.setCurrentRow(row),
            )
        self._apply_nav()

    def _apply_nav(self) -> None:
        try:
            hidden = self._repo.get_setting(NAV_HIDDEN) == "1"
        except Exception:  # a slot must not raise
            hidden = False
        self.nav.setVisible(not hidden)
        self.nav_toggle.setArrowType(Qt.ArrowType.RightArrow if hidden else Qt.ArrowType.LeftArrow)
        self.nav_toggle.setToolTip(
            "Show navigation (Ctrl+B)" if hidden else "Hide navigation (Ctrl+B)"
        )

    def toggle_nav(self) -> None:
        try:
            self._repo.set_setting(NAV_HIDDEN, "" if self.nav.isHidden() else "1")
        except Exception as exc:
            QMessageBox.warning(self, "Navigation bar", f"Could not store the setting: {exc}")
            return
        self._apply_nav()
        self.settings.refresh()

    def _ride(self, workout: Workout, unit_id: str | None = None) -> None:
        self.ride.set_workout(workout, unit_id)
        self.sidebar.setCurrentRow(RIDE)

    def _open_workout(self, workout_id: UUID) -> None:
        if self.editor.open_workout(workout_id):
            self.sidebar.setCurrentRow(LIBRARY)

    def _rides_changed(self, session_id: int | None) -> None:
        """session_id: the new ride; None: any number of them."""
        try:
            first, last = _FIRST_DAY, date.today()
            if session_id is not None:
                session = self._repo.get_session(session_id)
                if session is not None:
                    first = last = session.started_at_utc.astimezone().date()
            self._plans.auto_match(first, last)
        except Exception:  # a slot must not raise; the rides can be linked by hand
            log.exception("matching rides failed")
        self.plan.refresh()
        self.history.refresh()
        self.progress.refresh()

    def confirm_abort(self) -> bool:
        answer = QMessageBox.question(self, "Ride in progress", "Abort the ride and quit?")
        return answer == QMessageBox.StandardButton.Yes

    def closeEvent(self, event: QCloseEvent | None) -> None:
        assert event is not None
        if self.controller.is_active and not self.confirm_abort():
            event.ignore()
            return
        if not self.editor.confirm_discard() or not self.profile.confirm_discard():
            event.ignore()
            return
        self.controller.shutdown()
        if self.coach_panel is not None:
            self.coach_panel.shutdown()
        event.accept()
