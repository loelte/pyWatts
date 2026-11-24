"""Personal data and training zones."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from PyQt6.QtCore import QDate, Qt, pyqtSignal
from PyQt6.QtGui import QShowEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pywatts.core.zones import ZONE_COUNT, Zones, power_zones
from pywatts.storage.profile import Profile, estimate_max_hr, load_profile, save_profile
from pywatts.storage.repo import UNITS, Repo
from pywatts.ui import theme
from pywatts.ui.widgets.common import GAP, pad_spin_boxes, page_layout, text_label

FTP_MIN, FTP_MAX = 50, 600
LB_PER_KG = 2.2046226218
CM_PER_IN = 2.54
NOT_SET = "not set"


def _spin(high: int, suffix: str) -> QSpinBox:
    box = QSpinBox()
    box.setRange(0, high)
    box.setSuffix(suffix)
    box.setSpecialValueText(NOT_SET)
    return box


def _row(*widgets: QWidget) -> QHBoxLayout:
    box = QHBoxLayout()
    for i, widget in enumerate(widgets):
        box.addWidget(widget, 0 if i else 1)
    return box


class ZoneEditor(QGroupBox):
    """Five zones; the upper limits of Z1..Z4 can be edited with the custom method."""

    changed = pyqtSignal()

    def __init__(self, title: str, unit: str, high: int) -> None:
        super().__init__(title)
        self._unit = unit
        self.method = QComboBox()
        self.limits: list[QSpinBox] = []
        self.ranges: list[QLabel] = []
        grid = QGridLayout(self)
        grid.addWidget(self.method, 0, 0, 1, 3)
        for z in range(1, ZONE_COUNT + 1):
            name = QLabel(f" Z{z} ")
            name.setStyleSheet(
                f"background: {theme.ZONE_COLORS[z]}; color: {theme.ON_ZONE};"
                "font-weight: bold; border-radius: 6px; padding: 3px 4px;"
            )
            grid.addWidget(name, z, 0)
            label = text_label("body")
            self.ranges.append(label)
            grid.addWidget(label, z, 1)
            if z < ZONE_COUNT:
                box = QSpinBox()
                box.setRange(0, high)
                box.setPrefix("up to ")
                box.setSuffix(f" {unit}")
                box.valueChanged.connect(lambda _v: self.changed.emit())
                self.limits.append(box)
                grid.addWidget(box, z, 2)
        grid.setColumnStretch(1, 1)

    def show_zones(self, zones: Zones | None, editable: bool) -> None:
        for box in self.limits:
            box.setEnabled(editable)
        if zones is not None and not editable:
            self.fill(zones)
        for i, label in enumerate(self.ranges):
            if zones is None:
                label.setText("--")
                continue
            low, high = zones.ranges()[i]
            label.setText(f"{low}+ {self._unit}" if high is None else f"{low}–{high} {self._unit}")

    def fill(self, zones: Zones) -> None:
        for box, limit in zip(self.limits, zones.limits, strict=True):
            box.blockSignals(True)
            box.setValue(limit)
            box.blockSignals(False)

    def entered(self) -> Zones:
        """Raises ValueError."""
        return Zones(tuple(box.value() for box in self.limits))  # type: ignore[arg-type]


class ProfilePage(QWidget):
    profile_changed = pyqtSignal()

    def __init__(self, repo: Repo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._imperial = False
        self._loading = False
        self._stored = Profile(FTP_MIN)
        self._shown = (0.0, 0.0)  # displayed weight and height as loaded
        self.dirty = False
        self._build()
        self.refresh(force=True)

    def _build(self) -> None:
        self.name = QLineEdit()
        self.sex = QComboBox()
        for key in ("", "female", "male", "other"):
            self.sex.addItem(key.capitalize() or NOT_SET, key)
        self.has_birth = QCheckBox()
        self.birth = QDateEdit()
        self.birth.setCalendarPopup(True)
        self.birth.setDisplayFormat("yyyy-MM-dd")
        self.birth.setDateRange(QDate(1900, 1, 1), QDate.currentDate())
        self.age = text_label("muted")
        self.height_box = QDoubleSpinBox()
        self.weight = QDoubleSpinBox()
        for box in (self.height_box, self.weight):
            box.setDecimals(1)
            box.setSpecialValueText(NOT_SET)
        self.weight.setSingleStep(0.1)
        self.ftp = QSpinBox()
        self.ftp.setRange(FTP_MIN, FTP_MAX)
        self.ftp.setSuffix(" W")
        self.w_per_kg = text_label("muted")
        self.resting_hr = _spin(150, " bpm")
        self.max_hr = _spin(250, " bpm")
        self.threshold_hr = _spin(250, " bpm")
        self.estimate_button = QPushButton("Estimate from age")

        birth_row = QHBoxLayout()
        birth_row.addWidget(self.has_birth)
        birth_row.addWidget(self.birth, 1)
        birth_row.addWidget(self.age)
        person = QGroupBox("Personal")
        form = QFormLayout(person)
        form.addRow("Name", self.name)
        form.addRow("Sex", self.sex)
        form.addRow("Date of birth", birth_row)
        form.addRow("Height", self.height_box)
        form.addRow("Weight", self.weight)
        fitness = QGroupBox("Fitness")
        form = QFormLayout(fitness)
        form.addRow("FTP", _row(self.ftp, self.w_per_kg))
        form.addRow("Resting heart rate", self.resting_hr)
        form.addRow("Max heart rate", _row(self.max_hr, self.estimate_button))
        form.addRow("Threshold heart rate", self.threshold_hr)

        self.power = ZoneEditor("Power zones", "W", 2000)
        self.power.method.addItem("Calculated from FTP", "ftp")
        self.power.method.addItem("Custom", "custom")
        self.hr = ZoneEditor("Heart rate zones", "bpm", 250)
        self.hr.method.addItem("Percent of max heart rate", "max")
        self.hr.method.addItem("Percent of heart rate reserve", "reserve")
        self.hr.method.addItem("Custom", "custom")

        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.save_button = QPushButton("Save")
        theme.set_role(self.save_button, "primary")
        self.revert_button = QPushButton("Revert")

        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(GAP)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.addWidget(person, 0, 0)
        grid.addWidget(fitness, 0, 1)
        grid.addWidget(self.power, 1, 0)
        grid.addWidget(self.hr, 1, 1)
        grid.setRowStretch(2, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        buttons = QHBoxLayout()
        buttons.addWidget(self.message, 1)
        buttons.addWidget(self.revert_button)
        buttons.addWidget(self.save_button)
        layout = QVBoxLayout(self)
        page_layout(self, layout)
        layout.addWidget(text_label("title", "Profile"))
        layout.addWidget(scroll, 1)
        layout.addLayout(buttons)
        pad_spin_boxes(self)

        self.name.textEdited.connect(lambda _t: self._edited())
        self.sex.currentIndexChanged.connect(lambda _i: self._edited())
        self.has_birth.toggled.connect(lambda _on: self._edited())
        self.birth.dateChanged.connect(lambda _d: self._edited())
        for box in (
            self.height_box,
            self.weight,
            self.ftp,
            self.resting_hr,
            self.max_hr,
            self.threshold_hr,
        ):
            box.valueChanged.connect(lambda _v: self._edited())
        for editor in (self.power, self.hr):
            editor.method.currentIndexChanged.connect(lambda _i: self._edited())
            editor.changed.connect(self._edited)
        self.estimate_button.clicked.connect(lambda: self._estimate())
        self.save_button.clicked.connect(lambda: self.save())
        self.revert_button.clicked.connect(lambda: self.refresh(force=True))

    # --- public ---

    def refresh(self, force: bool = False) -> None:
        """Show stored values; unsaved edits are kept unless forced."""
        if not self.dirty or force:
            self._guard(self._load)

    def save(self) -> bool:
        return self._guard(self._save)

    def confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        answer = self.ask_unsaved()
        if answer == "save":
            return self.save()
        return answer == "discard"

    def ask_unsaved(self) -> str:
        """'save', 'discard' or 'cancel'."""
        b = QMessageBox.StandardButton
        answer = QMessageBox.question(
            self, "Unsaved changes", "Save changes to your profile?", b.Save | b.Discard | b.Cancel
        )
        return {b.Save: "save", b.Discard: "discard"}.get(answer, "cancel")

    # --- internals ---

    def showEvent(self, event: QShowEvent | None) -> None:
        super().showEvent(event)
        self.refresh()

    def _guard(self, action: Callable[[], None]) -> bool:
        """Never let a failure escape a slot: PyQt6 would abort the app."""
        self._info("")
        try:
            action()
        except ValueError as e:
            self._info(str(e), error=True)
            return False
        except Exception as e:
            self._info(f"{type(e).__name__}: {e}", error=True)
            return False
        return True

    def _info(self, text: str, error: bool = False) -> None:
        self.message.setStyleSheet(f"color: {theme.ERROR}" if error else "")
        self.message.setText(text)

    def _load(self) -> None:
        p = load_profile(self._repo)
        self._imperial = self._repo.get_setting(UNITS) == "imperial"
        self._loading = True
        try:
            # ranges first: setValue is clamped to them
            self.weight.setRange(0, 600 if self._imperial else 250)
            self.weight.setSuffix(" lb" if self._imperial else " kg")
            self.height_box.setRange(0, 100 if self._imperial else 250)
            self.height_box.setSuffix(" in" if self._imperial else " cm")
            self.name.setText(p.name)
            self.sex.setCurrentIndex(max(0, self.sex.findData(p.sex)))
            self.has_birth.setChecked(p.birth_date is not None)
            self.birth.setDate(QDate(p.birth_date) if p.birth_date else QDate(1990, 1, 1))
            self.weight.setValue(p.weight_kg * (LB_PER_KG if self._imperial else 1))
            self.height_box.setValue(p.height_cm / (CM_PER_IN if self._imperial else 1))
            self._stored = p
            self._shown = (self.weight.value(), self.height_box.value())
            self.ftp.setValue(p.ftp_w)
            self.resting_hr.setValue(p.resting_hr)
            self.max_hr.setValue(p.max_hr)
            self.threshold_hr.setValue(p.threshold_hr)
            self.power.method.setCurrentIndex(1 if p.custom_power else 0)
            self.power.fill(p.power_zones)
            self.hr.method.setCurrentIndex(max(0, self.hr.method.findData(p.hr_method)))
            if (hr := p.hr_zones) is not None:
                self.hr.fill(hr)
        finally:
            self._loading = False
        self.dirty = False
        self._update()

    def _entered(self) -> Profile:
        """Without custom zones (they may be invalid while typing)."""
        weight, height = self.weight.value(), self.height_box.value()
        # unchanged values keep the stored precision
        kg, cm = self._stored.weight_kg, self._stored.height_cm
        if weight != self._shown[0]:
            kg = round(weight / (LB_PER_KG if self._imperial else 1), 2)
        if height != self._shown[1]:
            cm = round(height * (CM_PER_IN if self._imperial else 1), 1)
        return Profile(
            ftp_w=self.ftp.value(),
            name=self.name.text().strip(),
            sex=self.sex.currentData(),
            birth_date=self.birth.date().toPyDate() if self.has_birth.isChecked() else None,
            height_cm=cm,
            weight_kg=kg,
            resting_hr=self.resting_hr.value(),
            max_hr=self.max_hr.value(),
            threshold_hr=self.threshold_hr.value(),
            hr_method=self.hr.method.currentData(),
        )

    def _save(self) -> None:
        p = self._entered()
        if p.max_hr and max(p.resting_hr, p.threshold_hr) >= p.max_hr:
            raise ValueError("Resting and threshold heart rate must be below the max heart rate")
        if self.power.method.currentData() == "custom":
            p = replace(p, custom_power=self._zones(self.power))
        if p.hr_method == "custom":
            p = replace(p, custom_hr=self._zones(self.hr))
        save_profile(self._repo, p)
        self._load()
        self._info("Saved")
        self.profile_changed.emit()

    @staticmethod
    def _zones(editor: ZoneEditor) -> Zones:
        try:
            return editor.entered()
        except ValueError as e:
            raise ValueError(f"{editor.title()}: {e}") from e

    def _edited(self) -> None:
        if not self._loading:
            self.dirty = True
            self._guard(self._update)

    def _update(self) -> None:
        p = self._entered()
        age = p.age()
        self.birth.setEnabled(self.has_birth.isChecked())
        self.age.setText("" if age is None else f"{age} years")
        self.estimate_button.setEnabled(age is not None)
        self.w_per_kg.setText(f"{p.ftp_w / p.weight_kg:.2f} W/kg" if p.weight_kg else "")
        self._update_zones(self.power, power_zones(p.ftp_w))
        self._update_zones(self.hr, p.hr_zones)
        self.save_button.setEnabled(self.dirty)
        self.revert_button.setEnabled(self.dirty)
        needs = {"max": "max heart rate", "reserve": "resting and max heart rate"}
        if p.hr_zones is None and p.hr_method in needs:
            self._info(f"Enter {needs[p.hr_method]} to get heart rate zones.")

    @staticmethod
    def _update_zones(editor: ZoneEditor, computed: Zones | None) -> None:
        custom = editor.method.currentData() == "custom"
        if custom:
            try:
                computed = editor.entered()
            except ValueError:
                computed = None
        editor.show_zones(computed, editable=custom)

    def _estimate(self) -> None:
        age = self._entered().age()
        if age is not None:
            self.max_hr.setValue(estimate_max_hr(age))
