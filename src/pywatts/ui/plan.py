"""Training plan: calendar of planned units and rides, beside the AI coach."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import Any

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QHideEvent, QResizeEvent, QShowEvent
from PyQt6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pywatts.core.plan import Plan, PlanUnit, Ride
from pywatts.storage.plan_repo import Item, PlanError, PlanRepo
from pywatts.storage.profile import load_profile
from pywatts.storage.repo import COACH_SPLIT, Repo
from pywatts.ui import theme
from pywatts.ui.widgets import calendar_model as cm
from pywatts.ui.widgets.calendar_model import CalendarData, Entry
from pywatts.ui.widgets.calendar_views import CalendarView, Legend, MonthView, WeekView, YearView
from pywatts.ui.widgets.common import GAP, page_layout, text_label
from pywatts.ui.widgets.plan_detail import DetailPanel
from pywatts.ui.widgets.plan_dialogs import DateDialog, PlanDialog, UnitDialog

DEFAULT_SPLIT = (660, 420)
SPLIT_SAVE_MS = 500
SIDE_FROM = 860  # px of calendar width from which the details fit beside it
MAX_TIMER_MS = 10 * 60 * 1000  # the clock can jump (sleep, time zone), so look again
_ONE_DAY = timedelta(days=1)


def ms_to_midnight(now: datetime) -> int:
    """Until shortly after the local midnight that follows `now` (aware)."""
    local = now.astimezone()
    midnight = datetime.combine(local.date() + _ONE_DAY, time()).astimezone()
    return int((midnight - local).total_seconds() * 1000) + 1000


def parse_split(text: str) -> tuple[int, int] | None:
    try:
        left, right = (int(part) for part in text.split(","))
    except ValueError:
        return None
    return (left, right) if 0 < left < 20_000 and 0 <= right < 20_000 else None


class _CalendarPane(QWidget):
    resized = pyqtSignal(int)  # width

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        super().resizeEvent(event)
        self.resized.emit(self.width())


class PlanPage(QWidget):
    ride_requested = pyqtSignal(object, str)  # Workout, unit id
    workout_requested = pyqtSignal(object)  # UUID of a library workout
    plan_changed = pyqtSignal()  # by the user

    def __init__(
        self,
        repo: Repo,
        plans: PlanRepo,
        coach_panel: QWidget | None = None,
        today: Callable[[], date] = date.today,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._repo, self._plans, self._today = repo, plans, today
        self._coach = coach_panel
        self._view = "month"
        self._anchor = today()
        self._shown_today = self._anchor  # what the colors are based on
        self._selected: tuple[str, str] | None = None
        self._plan_id: str | None = None  # filter
        self._plan_names: dict[str, str] = {}
        self._loads = cm.Loads()
        self._titles: dict[Ride, str] = {}
        self._links: dict[str, int] = {}
        self._hows: dict[str, str] = {}
        self._data = CalendarData(self._anchor, self._anchor, self._anchor)
        self._build()
        self._split_timer = QTimer(self, singleShot=True, interval=SPLIT_SAVE_MS)
        self._split_timer.timeout.connect(self._slot(self._save_split, clear=False))
        self._day_timer = QTimer(self, singleShot=True)
        self._day_timer.timeout.connect(self._slot(self.check_day, clear=False))
        self._guard(self._restore_split)
        self._start_day_timer()

    # --- overridable dialogs ---
    def ask_unit(self, day: date, unit: PlanUnit | None = None) -> PlanUnit | None:
        """A new unit for this day, or the changed `unit`."""
        dialog = UnitDialog(
            day,
            unit,
            self._repo.list_workouts(),
            self._plans.list_plans(),
            self._plan_id,
            unit is not None and unit.id in self._plans.links(),
            self._loads,
            self,
        )
        return self._exec(dialog)

    def ask_plan(self, plan: Plan | None = None) -> Plan | None:
        return self._exec(PlanDialog(plan, cm.clamp(self._today()), self))

    def ask_date(self, title: str, day: date) -> date | None:
        return self._exec(DateDialog(title, day, self))

    def ask_confirm(self, text: str) -> bool:
        b = QMessageBox.StandardButton
        box = QMessageBox(QMessageBox.Icon.Question, "Please confirm", text, b.Yes | b.No, self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        answer = box.exec()
        box.deleteLater()
        return answer == b.Yes

    def pick(self, title: str, label: str, texts: Sequence[str]) -> int | None:
        """Index of the chosen text."""
        numbered = [f"{i}. {text}" for i, text in enumerate(texts, start=1)]  # may be equal
        text, ok = QInputDialog.getItem(self, title, label, numbered, 0, False)
        return numbered.index(text) if ok else None

    @staticmethod
    def _exec(dialog: Any) -> Any:
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        value = dialog.value() if accepted else None
        dialog.deleteLater()
        return value

    # --- public ---
    @property
    def view(self) -> str:
        return self._view

    @property
    def anchor(self) -> date:
        """A day of the shown period."""
        return self._anchor

    @property
    def calendar(self) -> CalendarView:
        return self.views[self._view]

    def refresh(self) -> None:
        """Re-read everything; view, period and selection stay. Waits until the page is shown."""
        if self.isVisible():
            self._guard(self._reload, clear=False)

    def show_period(self, view: str, day: date) -> None:
        self._guard(lambda: self._show_period(view, day))

    def check_day(self) -> None:
        """Runs at midnight, and after sleep or a clock change within minutes."""
        self._start_day_timer()
        if self._today() != self._shown_today:
            self.refresh()

    # --- build ---
    def _build(self) -> None:
        self.add_button = QPushButton("Add unit")
        theme.set_role(self.add_button, "primary")
        self.add_button.clicked.connect(self._slot(lambda: self._add(self._day_for_new())))
        self.plan_combo = QComboBox()
        self.plan_combo.setAccessibleName("Shown plan")
        self.plan_combo.setMinimumContentsLength(9)
        self.plan_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.plan_combo.addItem("All plans", None)
        self.plan_combo.activated.connect(self._slot(self._filter_chosen))
        self.plan_button = QPushButton("Plans")
        theme.set_role(self.plan_button, "menu")
        menu = QMenu(self.plan_button)
        self.actions: dict[str, QAction] = {}
        for key, text, action in (
            ("new", "New plan", self._new_plan),
            ("edit", "Edit plan", self._edit_plan),
            ("archive", "Archive plan", self._archive_plan),
            ("restore", "Restore plan", self._restore_plan),
        ):
            item = QAction(text, menu)
            item.triggered.connect(self._slot(action))
            menu.addAction(item)
            self.actions[key] = item
        self.plan_button.setMenu(menu)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(text_label("title", "Training Plan"), 1)
        top.addWidget(self.plan_combo)
        top.addWidget(self.plan_button)
        self.coach_button = QPushButton("Coach")
        self.coach_button.setToolTip("Show or hide the AI coach")
        self.coach_button.clicked.connect(self._slot(self._toggle_coach))
        self.coach_button.setVisible(self._coach is not None)
        top.addWidget(self.coach_button)
        top.addWidget(self.add_button)

        group = QButtonGroup(self)  # exclusive
        switch = QHBoxLayout()
        switch.setSpacing(0)
        self.view_buttons: dict[str, QPushButton] = {}
        for view, end in zip(cm.VIEWS, ("left", "", "right"), strict=True):
            button = QPushButton(view.capitalize())
            button.setCheckable(True)
            button.setChecked(view == self._view)
            theme.set_role(button, end, "end")
            theme.set_role(button, "segment")
            button.clicked.connect(self._slot(partial(self._show_period, view, None)))
            group.addButton(button)
            switch.addWidget(button)
            self.view_buttons[view] = button
        self.previous_button, self.next_button = QPushButton("‹"), QPushButton("›")
        self.today_button = QPushButton("Today")
        for button, name, step in (
            (self.previous_button, "Previous", -1),
            (self.next_button, "Next", 1),
        ):
            theme.set_role(button, "icon")
            button.setToolTip(name)
            button.setAccessibleName(name)
            button.clicked.connect(self._slot(partial(self._step, step)))
        self.today_button.clicked.connect(
            self._slot(lambda: self._show_period(self._view, self._today()))
        )
        self.period_label = text_label("heading")
        nav = QHBoxLayout()
        nav.setSpacing(6)
        nav.addLayout(switch)
        nav.addSpacing(6)
        nav.addWidget(self.previous_button)
        nav.addWidget(self.today_button)
        nav.addWidget(self.next_button)
        nav.addSpacing(6)
        nav.addWidget(self.period_label, 1)

        self.message = text_label("error")
        self.message.setWordWrap(True)
        self.message.hide()
        self.summary_label = text_label("muted")

        self.views: dict[str, CalendarView] = {
            "year": YearView(),
            "month": MonthView(),
            "week": WeekView(),
        }
        self.stack = QStackedWidget()
        self.stack.setMinimumHeight(120)
        self._scroll_timer = QTimer(self, singleShot=True, interval=0)
        self._scroll_timer.timeout.connect(self._slot(self._scroll_to_selected, clear=False))
        for view in self.views.values():
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            scroll.setWidget(view)
            scroll.viewport().setAutoFillBackground(False)
            view.setAutoFillBackground(False)
            self.stack.addWidget(scroll)
            view.entry_clicked.connect(self._item_slot(self._select))
            view.entry_activated.connect(self._item_slot(self._activate))
            view.day_activated.connect(self._item_slot(self._add))
            view.day_opened.connect(self._item_slot(self._open_day))
            view.entry_moved.connect(self._item_slot(self._move_entry))
            view.move_refused.connect(self._item_slot(self._refuse_move))
            view.cursor_left.connect(self._item_slot(self._follow_cursor))
        self.stack.setCurrentIndex(list(self.views).index(self._view))

        self.detail = DetailPanel()
        self.detail.hide()
        self.detail.closed.connect(self._slot(self._close_detail))
        for key, action in (
            ("ride", self._ride),
            ("library", self._open_workout),
            ("edit", self._edit),
            ("move", self._move),
            ("skip", self._skip),
            ("link", self._link),
            ("delete", self._delete),
        ):
            self.detail.buttons[key].clicked.connect(self._slot(action))
        self._body = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self._body.setSpacing(GAP)
        self._body.addWidget(self.stack, 1)
        self._body.addWidget(self.detail)

        pane = _CalendarPane()
        left = QVBoxLayout(pane)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(10)
        left.addLayout(top)
        left.addLayout(nav)
        left.addWidget(self.message)
        left.addWidget(self.summary_label)
        left.addLayout(self._body, 1)
        left.addWidget(Legend())
        pane.resized.connect(self._place_detail)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(pane)
        self.splitter.setCollapsible(0, False)
        self.splitter.setStretchFactor(0, 1)
        if self._coach is not None:
            self.splitter.addWidget(self._coach)
            self.splitter.setCollapsible(1, True)
            self.splitter.setStretchFactor(1, 0)
            self.splitter.setHandleWidth(20)
            self.splitter.splitterMoved.connect(lambda *_: self._split_timer.start())
        layout = QVBoxLayout(self)
        page_layout(self, layout)
        layout.addWidget(self.splitter)

    # --- guard ---
    def _guard(self, action: Callable[[], object], clear: bool = True) -> bool:
        """Run slot work; a failure is shown, never raised. clear: drop the last message."""
        if clear:
            self._say("")
        try:
            action()
            return True
        except (PlanError, ValueError) as e:
            self._say(str(e))
        except Exception as e:
            self._say(f"{type(e).__name__}: {e}")
        return False

    def _slot(self, action: Callable[[], object], clear: bool = True) -> Callable[..., object]:
        """Guarded; drops the arguments of the signal, like the state of clicked()."""
        return lambda *_args: self._guard(action, clear)

    def _item_slot(self, action: Callable[..., object]) -> Callable[..., object]:
        """Guarded, with the arguments of the signal."""
        return lambda *args: self._guard(partial(action, *args))

    def _say(self, text: str) -> None:
        self.message.setText(text)
        self.message.setVisible(bool(text))

    # --- loading ---
    def showEvent(self, event: QShowEvent | None) -> None:
        super().showEvent(event)
        self.refresh()

    def hideEvent(self, event: QHideEvent | None) -> None:
        super().hideEvent(event)
        if self._split_timer.isActive():  # the app may be closing
            self._split_timer.stop()
            self._guard(self._save_split, clear=False)

    def _reload(self) -> None:
        self._load_plans()
        today = self._today()
        first, last = cm.visible_range(self._view, self._anchor)
        links = self._plans.links()
        self._links = {unit_id: link.session_id for unit_id, link in links.items()}
        self._hows = {unit_id: link.how for unit_id, link in links.items()}
        units = self._plans.units_between(first, last, self._plan_id)
        # a day more: the linked ride may be on the day before or after
        rides = {
            r.session_id: r for r in self._plans.rides_between(first - _ONE_DAY, last + _ONE_DAY)
        }
        for unit in units:  # ridden on quite another day
            session_id = self._links.get(unit.id)
            if session_id is not None and session_id not in rides:
                ride = self._ride_of(session_id)
                if ride is not None:
                    rides[session_id] = ride
        self._data = cm.build(
            first,
            last,
            today,
            units,
            self._links,
            rides,
            self._loads,
            self._title_of,
            self._plan_id is not None,
        )
        self._shown_today = today
        profile = load_profile(self._repo)
        self.calendar.set_data(self._data, self._anchor, profile.ftp_w, profile.power_zones)
        self.period_label.setText(cm.period_label(self._view, self._anchor))
        totals = self._data.totals(*cm.period(self._view, self._anchor))
        self.summary_label.setText(totals.text())
        self._show_detail()

    def _load_plans(self) -> None:
        plans = self._plans.list_plans()
        self._plan_names = {p.id: p.name for p in plans}
        if self._plan_id not in self._plan_names:
            self._plan_id = None
        items = [("All plans", None), *((cm.one_line(p.name), p.id) for p in plans)]
        combo = self.plan_combo
        if items != [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]:
            combo.clear()
            for text, plan_id in items:
                combo.addItem(text, plan_id)
        index = 0 if self._plan_id is None else self.plan_combo.findData(self._plan_id)
        self.plan_combo.setCurrentIndex(index)
        for key in ("edit", "archive"):
            self.actions[key].setEnabled(bool(plans))

    def _ride_of(self, session_id: int) -> Ride | None:
        """None while it is still being recorded."""
        session = self._repo.get_session(session_id)
        if session is None or session.ended_at_utc is None:
            return None
        day = session.started_at_utc.astimezone().date()
        return Ride(session.id, day, session.duration_s, session.load)

    def _title_of(self, ride: Ride) -> str:
        if ride not in self._titles:
            session = self._repo.get_session(ride.session_id)
            self._titles[ride] = "" if session is None else session.workout.name
        return self._titles[ride]

    # --- period ---
    def _show_period(self, view: str, day: date | None = None) -> None:
        """day None: switch the view, around the day that was clicked last."""
        if day is None:
            cursor = self.calendar.cursor_day
            inside = self._data.first <= cursor <= self._data.last
            day = cursor if inside and view != self._view else self._anchor
        self._view = view
        self._anchor = cm.clamp(day)
        self.calendar.set_cursor_day(self._anchor)
        self.view_buttons[view].setChecked(True)
        self.stack.setCurrentIndex(list(self.views).index(view))
        self._reload()
        self.calendar.set_selected(self._selected)

    def _step(self, step: int) -> None:
        self._show_period(self._view, cm.shift(self._view, self._anchor, step))

    def _open_day(self, day: date) -> None:
        """The next closer view."""
        self._show_period("month" if self._view == "year" else "week", day)
        self.calendar.setFocus()

    def _follow_cursor(self, day: date) -> None:
        self._show_period(self._view, day)

    def _filter_chosen(self) -> None:
        self._plan_id = self.plan_combo.currentData()
        self._reload()

    def _day_for_new(self) -> date:
        """Where the calendar was clicked last, else today."""
        first, last = cm.period(self._view, self._anchor)
        day = self.calendar.cursor_day
        if self._view != "year" and first <= day <= last:
            return day
        today = self._today()
        return today if first <= today <= last else first

    def _start_day_timer(self) -> None:
        wait = ms_to_midnight(datetime.now().astimezone())
        self._day_timer.start(min(wait, MAX_TIMER_MS))

    # --- detail ---
    def _select(self, entry: Entry) -> None:
        self._selected = entry.key
        self._show_detail()
        self._scroll_timer.start()  # once the panel has its place

    def _scroll_to_selected(self) -> None:
        rect = None if self._selected is None else self.calendar.rect_of(self._selected)
        scroll = self.stack.currentWidget()
        if rect is not None and isinstance(scroll, QScrollArea):
            center = rect.center().toPoint()
            scroll.ensureVisible(center.x(), center.y(), 0, round(rect.height() / 2) + 8)

    def _activate(self, entry: Entry) -> None:
        self._select(entry)
        if entry.unit is not None:
            self._edit()

    def _close_detail(self) -> None:
        self._selected = None
        self._show_detail()

    def _show_detail(self) -> None:
        entry = self._find(self._selected)
        if entry is None:
            self._selected = None
        self.calendar.set_selected(self._selected)
        if entry is None:
            self.detail.hide()
            return
        session = None
        if entry.ride is not None:
            session = self._repo.get_session(entry.ride.session_id)
        unit = entry.unit
        if unit is None:
            self.detail.show_ride(entry, session)
        else:
            workout = unit.workout
            in_library = workout is not None and self._plans.workout_origin(workout.id) is not None
            profile = load_profile(self._repo)
            self.detail.show_unit(
                entry,
                self._plan_name(unit.plan_id),
                session,
                self._hows.get(unit.id),
                in_library,
                profile.ftp_w,
                profile.power_zones,
            )
        self.detail.show()

    def _plan_name(self, plan_id: str | None) -> str | None:
        if plan_id is None or plan_id in self._plan_names:
            return self._plan_names.get(plan_id or "")
        plan = self._plans.get_plan(plan_id)
        return None if plan is None else plan.name

    def _find(self, key: tuple[str, str] | None) -> Entry | None:
        """Also outside of the shown period."""
        if key is None:
            return None
        entry = self._data.find(key)
        if entry is not None:
            return entry
        kind, value = key
        if kind == "unit":
            unit = self._plans.get_unit(value)
            if unit is None:
                return None
            session_id = self._links.get(unit.id)
            ride = None if session_id is None else self._ride_of(session_id)
            return cm.make_entry(unit, ride, session_id is not None, self._shown_today, self._loads)
        ride = self._ride_of(int(value))
        if ride is None or ride.session_id in self._links.values():
            return None
        return Entry(ride.day, cm.one_line(self._title_of(ride)) or "Ride", ride=ride)

    def _place_detail(self, width: int) -> None:
        side = width >= SIDE_FROM
        d = QBoxLayout.Direction
        self._body.setDirection(d.LeftToRight if side else d.TopToBottom)
        self.detail.set_side(side)

    # --- changing ---
    def _write(
        self, summary: str, saves: Sequence[Item] = (), deletes: Sequence[Item] = ()
    ) -> None:
        self._plans.apply("user", summary, saves, deletes)
        days = [u.day for u in saves if isinstance(u, PlanUnit) and u.day <= self._today()]
        try:
            if days:  # a ride of that day may fit now
                self._plans.auto_match(min(days), max(days))
        finally:
            self._changed()

    def _changed(self) -> None:
        try:
            self._reload()
        finally:
            self.plan_changed.emit()

    def _current_unit(self) -> PlanUnit:
        """As it is stored now: the AI may have changed it since it was shown."""
        entry = self.detail.entry
        unit = None
        if self._selected is not None and entry is not None and entry.unit is not None:
            unit = self._plans.get_unit(entry.unit.id)
        if unit is None:
            raise PlanError("This unit no longer exists.")
        return unit

    def _unchanged(self, unit: PlanUnit) -> None:
        """Dialogs take time, and the AI can make changes meanwhile."""
        if self._plans.get_unit(unit.id) != unit:
            raise PlanError("This unit was changed in the meantime. Please try again.")

    def _next_position(self, day: date, unit_id: str) -> int:
        others = [u.position for u in self._plans.units_between(day, day) if u.id != unit_id]
        return max(others, default=-1) + 1

    def _add(self, day: date) -> None:
        unit = self.ask_unit(cm.clamp(day))
        if unit is None:
            return
        unit = replace(unit, position=self._next_position(unit.day, unit.id))
        self._selected = ("unit", unit.id)
        self._write(f"Added “{unit.title}” on {unit.day}", [unit])

    def _edit(self) -> None:
        old = self._current_unit()
        new = self.ask_unit(old.day, old)
        if new is None or new == old:
            return
        self._unchanged(old)
        if new.day != old.day:
            self._check_movable(old)
            new = replace(new, position=self._next_position(new.day, new.id))
        self._write(f"Changed “{new.title}” on {new.day}", [new])

    def _move(self) -> None:
        unit = self._current_unit()
        self._check_movable(unit)
        day = self.ask_date(f"Move “{cm.one_line(unit.title)}”", unit.day)
        if day is not None:
            self._unchanged(unit)
            self._move_to(unit, day)

    def _move_entry(self, entry: Entry, day: date) -> None:
        if entry.unit is None:
            return
        unit = self._plans.get_unit(entry.unit.id)
        if unit is None:
            raise PlanError("This unit no longer exists.")
        self._move_to(unit, day)

    def _move_to(self, unit: PlanUnit, day: date) -> None:
        if day == unit.day:
            return
        self._check_movable(unit)
        moved = replace(unit, day=day, position=self._next_position(day, unit.id))
        self._write(f"Moved “{unit.title}” from {unit.day} to {day}", [moved])

    def _check_movable(self, unit: PlanUnit) -> None:
        if unit.id in self._plans.links():
            raise PlanError("This unit is linked to a ride and cannot be moved. Unlink it first.")

    def _refuse_move(self, entry: Entry) -> None:
        raise PlanError(
            f"“{entry.title}” is linked to a ride and cannot be moved. Unlink it first."
        )

    def _skip(self) -> None:
        unit = self._current_unit()
        verb = "Unskipped" if unit.skipped else "Skipped"
        self._write(
            f"{verb} “{unit.title}” on {unit.day}", [replace(unit, skipped=not unit.skipped)]
        )

    def _delete(self) -> None:
        unit = self._current_unit()
        if not self.ask_confirm(f"Delete “{cm.one_line(unit.title)}” on {cm.day_text(unit.day)}?"):
            return
        self._unchanged(unit)
        self._write(f"Deleted “{unit.title}” on {unit.day}", deletes=[unit])

    def _ride(self) -> None:
        unit = self._current_unit()
        if unit.workout is None or not unit.workout.segments:
            raise PlanError("This unit has no workout to ride.")
        self.ride_requested.emit(unit.workout, unit.id)

    def _open_workout(self) -> None:
        workout = self._current_unit().workout
        if workout is None or self._repo.get_workout(workout.id) is None:
            raise PlanError("This workout is no longer in the library.")
        self.workout_requested.emit(workout.id)

    def _link(self) -> None:
        unit = self._current_unit()
        links = self._plans.links()
        if unit.id in links:
            self._plans.unlink(unit.id)
            self._changed()
            return
        taken = {link.session_id for link in links.values()}
        rides = [
            r
            for r in self._plans.rides_between(unit.day - _ONE_DAY, unit.day + _ONE_DAY)
            if r.session_id not in taken
        ]
        if not rides:
            raise PlanError("There is no free ride within a day of this unit.")
        texts = [
            f"{cm.day_text(r.day)}: {cm.one_line(self._title_of(r)) or 'Ride'}, "
            f"{cm.hours(r.duration_s)}, load {cm.number(r.load)}"
            for r in rides
        ]
        index = self.pick("Link ride", "Which ride belongs to this unit?", texts)
        if index is not None:
            self._plans.link(unit.id, rides[index].session_id, "manual")
            self._changed()

    # --- plans ---
    def _choose_plan(self, plans: list[Plan], title: str) -> Plan | None:
        """The filtered plan, else the only one, else the rider decides."""
        for plan in plans:
            if plan.id == self._plan_id:
                return plan
        if len(plans) < 2:
            return plans[0] if plans else None
        index = self.pick(title, "Which plan?", [cm.one_line(p.name) for p in plans])
        return None if index is None else plans[index]

    def _new_plan(self) -> None:
        plan = self.ask_plan()
        if plan is not None:
            self._plan_id = plan.id
            self._write(f"Created the plan “{plan.name}”", [plan])

    def _edit_plan(self) -> None:
        old = self._choose_plan(self._plans.list_plans(), "Edit plan")
        new = None if old is None else self.ask_plan(old)
        if old is None or new is None or new == old:
            return
        self._unchanged_plan(old)
        self._write(f"Changed the plan “{new.name}”", [new])

    def _archive_plan(self) -> None:
        plan = self._choose_plan(self._plans.list_plans(), "Archive plan")
        if plan is None or not self.ask_confirm(
            f"Archive “{cm.one_line(plan.name)}”? Units that were not ridden are hidden then."
        ):
            return
        self._unchanged_plan(plan)
        self._write(f"Archived the plan “{plan.name}”", [replace(plan, archived=True)])

    def _restore_plan(self) -> None:
        plans = [p for p in self._plans.list_plans(archived=True) if p.archived]
        if not plans:
            raise PlanError("There is no archived plan.")
        index = self.pick("Restore plan", "Which plan?", [cm.one_line(p.name) for p in plans])
        if index is not None:
            plan = plans[index]
            self._unchanged_plan(plan)
            self._write(f"Restored the plan “{plan.name}”", [replace(plan, archived=False)])

    def _unchanged_plan(self, plan: Plan) -> None:
        if self._plans.get_plan(plan.id) != plan:
            raise PlanError("This plan was changed in the meantime. Please try again.")

    # --- splitter ---
    def _restore_split(self) -> None:
        if self._coach is not None:
            sizes = parse_split(self._repo.get_setting(COACH_SPLIT))
            self.splitter.setSizes(list(sizes or DEFAULT_SPLIT))

    def _save_split(self) -> None:
        left, right = self.splitter.sizes()
        self._repo.set_setting(COACH_SPLIT, f"{left},{right}")

    def _toggle_coach(self) -> None:
        left, right = self.splitter.sizes()
        if right:
            self.splitter.setSizes([left + right, 0])
        else:
            right = min(DEFAULT_SPLIT[1], (left + right) // 2)
            self.splitter.setSizes([left - right, right])
        self._split_timer.start()
