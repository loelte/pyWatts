"""Dialogs of the training plan. They only collect input; nothing is stored here."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta

from PyQt6.QtCore import QDate
from PyQt6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pywatts.core.plan import KINDS, MAX_TARGET_LOAD, MAX_TARGET_S, MAX_TITLE, Plan, PlanUnit
from pywatts.core.workout import Workout, total_duration
from pywatts.storage.plan_repo import new_id
from pywatts.ui.widgets.calendar_model import FIRST_DAY, LAST_DAY, Loads, hours, number, one_line
from pywatts.ui.widgets.common import GAP, pad_spin_boxes, text_label

DEFAULT_PLAN_DAYS = 28


def day_edit(day: date) -> QDateEdit:
    edit = QDateEdit()
    edit.setCalendarPopup(True)
    edit.setDisplayFormat("yyyy-MM-dd")
    edit.setDateRange(QDate(FIRST_DAY), QDate(LAST_DAY))
    edit.setDate(QDate(day))
    return edit


class FormDialog(QDialog):
    """A form with OK and Cancel; invalid input keeps it open and shows why."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self.form = QFormLayout()
        self.form.setSpacing(10)
        self.message = text_label("error")
        self.message.setWordWrap(True)
        self.message.hide()
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(GAP)
        layout.addLayout(self.form)
        layout.addWidget(self.message)
        layout.addWidget(self.buttons)

    def check(self) -> None:
        """Raises ValueError with a message for the user."""

    def accept(self) -> None:
        try:
            self.check()
        except ValueError as e:
            text = str(e)
            self.message.setText(text[:1].upper() + text[1:] + ".")
            self.message.show()
            return
        super().accept()


class DateDialog(FormDialog):
    def __init__(self, title: str, day: date, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.setMinimumWidth(280)
        self.day = day_edit(day)
        self.form.addRow("Date", self.day)

    def value(self) -> date:
        return self.day.date().toPyDate()


class PlanDialog(FormDialog):
    def __init__(self, plan: Plan | None, start: date, parent: QWidget | None = None) -> None:
        super().__init__("New plan" if plan is None else "Edit plan", parent)
        self._plan = plan
        self.name = QLineEdit("" if plan is None else plan.name)
        self.name.setMaxLength(MAX_TITLE)
        self.goal = QLineEdit("" if plan is None else plan.goal)
        end = min(LAST_DAY, start + timedelta(days=DEFAULT_PLAN_DAYS - 1))
        self.start = day_edit(start if plan is None else plan.start_day)
        self.end = day_edit(end if plan is None else plan.end_day)
        self.notes = QPlainTextEdit("" if plan is None else plan.notes)
        self.notes.setTabChangesFocus(True)
        self.notes.setFixedHeight(90)
        self.form.addRow("Name", self.name)
        self.form.addRow("Goal", self.goal)
        self.form.addRow("First day", self.start)
        self.form.addRow("Last day", self.end)
        self.form.addRow("Notes", self.notes)

    def check(self) -> None:
        self.value()

    def value(self) -> Plan:
        values = {
            "name": self.name.text().strip(),
            "goal": self.goal.text().strip(),
            "start_day": self.start.date().toPyDate(),
            "end_day": self.end.date().toPyDate(),
            "notes": self.notes.toPlainText(),
        }
        if self._plan is None:
            return Plan(id=new_id(), **values)
        return replace(self._plan, **values)


class UnitDialog(FormDialog):
    """New or changed unit. Linked to a ride: day and kind stay as they are."""

    def __init__(
        self,
        day: date,
        unit: PlanUnit | None,
        workouts: Sequence[Workout],
        plans: Sequence[Plan],
        plan_id: str | None = None,
        linked: bool = False,
        loads: Loads | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Add unit" if unit is None else "Edit unit", parent)
        self._unit = unit
        self._loads = loads or Loads()
        self._auto_title = unit is None
        self._shown = (0, 0.0)
        self.kind = QComboBox()
        for kind in KINDS:
            self.kind.addItem(kind.capitalize(), kind)
        self.day = day_edit(day)
        self.title = QLineEdit()
        self.title.setMaxLength(MAX_TITLE)
        self.workout = QComboBox()
        self.workout.setMinimumContentsLength(20)
        self.workout.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._workouts: list[Workout | None] = [None]
        self.workout.addItem("No workout")
        own = None if unit is None else unit.workout
        if own is not None and all(w.id != own.id for w in workouts):
            self._add_workout(own, " (no longer in the library)")
        for workout in workouts:  # the own one may be older than that of the library
            self._add_workout(own if own is not None and workout.id == own.id else workout)
        self.info = text_label("muted")
        self.duration = QSpinBox()
        self.duration.setRange(0, MAX_TARGET_S // 60)
        self.duration.setSingleStep(5)
        self.duration.setSuffix(" min")
        self.duration.setSpecialValueText("not set")
        self.load = QDoubleSpinBox()
        self.load.setRange(0, MAX_TARGET_LOAD)
        self.load.setDecimals(0)
        self.load.setSingleStep(5)
        self.load.setSpecialValueText("not set")
        self.plan = QComboBox()
        self.plan.addItem("No plan", None)
        for plan in plans:
            self.plan.addItem(plan.name, plan.id)
        self.notes = QPlainTextEdit()
        self.notes.setTabChangesFocus(True)
        self.notes.setFixedHeight(90)
        for label, field in (
            ("Kind", self.kind),
            ("Title", self.title),
            ("Date", self.day),
            ("Workout", self.workout),
            ("", self.info),
            ("Duration", self.duration),
            ("Load", self.load),
            ("Plan", self.plan),
            ("Notes", self.notes),
        ):
            self.form.addRow(label, field)
        pad_spin_boxes(self)
        self._fill(unit, plan_id)
        for field in (self.kind, self.day):
            field.setEnabled(not linked)
            if linked:
                field.setToolTip("Linked to a ride: unlink it first.")
        self.kind.currentIndexChanged.connect(lambda _i: self._show())
        self.workout.currentIndexChanged.connect(lambda _i: self._show())
        self.title.textEdited.connect(self._title_edited)
        self._show()
        self.title.setFocus()

    def _add_workout(self, workout: Workout, suffix: str = "") -> None:
        self._workouts.append(workout)
        self.workout.addItem(one_line(workout.name) + suffix)

    def _fill(self, unit: PlanUnit | None, plan_id: str | None) -> None:
        wanted = plan_id if unit is None else unit.plan_id
        if wanted is not None and self.plan.findData(wanted) < 0:
            self.plan.addItem("(archived plan)", wanted)
        self.plan.setCurrentIndex(self.plan.findData(wanted) if wanted is not None else 0)
        if unit is None:
            return
        self.kind.setCurrentIndex(self.kind.findData(unit.kind))
        self.title.setText(unit.title)
        self.notes.setPlainText(unit.notes)
        self.duration.setValue(0 if unit.target_s is None else max(1, round(unit.target_s / 60)))
        self.load.setValue(0 if unit.target_load is None else max(1, round(unit.target_load)))
        if unit.workout is not None:
            ids = [None if w is None else w.id for w in self._workouts]
            self.workout.setCurrentIndex(ids.index(unit.workout.id))
        # A value the fields cannot show exactly is kept unless changed.
        self._shown = (self.duration.value(), self.load.value())

    def _title_edited(self, text: str) -> None:
        self._auto_title = self._unit is None and not text

    def _selected_workout(self) -> Workout | None:
        if self.kind.currentData() != "indoor":
            return None
        return self._workouts[self.workout.currentIndex()]

    def _show(self) -> None:
        kind = self.kind.currentData()
        workout = self._selected_workout()
        self.form.setRowVisible(self.workout, kind == "indoor")
        self.form.setRowVisible(self.info, workout is not None)
        rideable = kind in ("indoor", "outdoor") and workout is None
        self.form.setRowVisible(self.duration, rideable)
        self.form.setRowVisible(self.load, rideable)
        if workout is not None:
            load = number(self._loads.of_workout(workout))
            self.info.setText(f"{hours(total_duration(workout))}, load {load}")
        if self._auto_title:
            suggested = "Rest day" if kind == "rest" else workout.name if workout else ""
            self.title.setText(one_line(suggested)[:MAX_TITLE])

    def check(self) -> None:
        self.value()

    def value(self) -> PlanUnit:
        workout = self._selected_workout()
        old = self._unit
        target_s: int | None = self.duration.value() * 60 or None
        target_load: float | None = self.load.value() or None
        if old is not None:
            if self.duration.value() == self._shown[0]:
                target_s = old.target_s
            if self.load.value() == self._shown[1]:
                target_load = old.target_load
        if self.kind.currentData() not in ("indoor", "outdoor"):
            target_s = target_load = None
        values = {
            "day": self.day.date().toPyDate(),
            "kind": self.kind.currentData(),
            "title": self.title.text().strip(),
            "notes": self.notes.toPlainText(),
            "plan_id": self.plan.currentData(),
            "workout": workout,
            "target_s": target_s,
            "target_load": target_load,
        }
        if old is None:
            return PlanUnit(id=new_id(), **values)
        return replace(old, **values)
