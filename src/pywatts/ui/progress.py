"""Progress: best powers, training volume and the log of body metrics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import QDate, QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QShowEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pywatts.core.metrics import PEAK_DURATIONS, estimated_load, fitness_form
from pywatts.storage.profile import load_profile
from pywatts.storage.repo import (
    FTP_W,
    MAX_HR,
    RESTING_HR,
    UNITS,
    WEIGHT_KG,
    Metric,
    Peak,
    Repo,
    SessionRow,
)
from pywatts.ui import theme
from pywatts.ui.profile import FTP_MAX, FTP_MIN, LB_PER_KG
from pywatts.ui.ride import NO_VALUE
from pywatts.ui.widgets.common import GAP, card, pad_spin_boxes, page_layout, text_label
from pywatts.ui.widgets.power_curve import (
    FormPlot,
    PowerCurvePlot,
    TrendPlot,
    WeeklyPlot,
    duration_label,
)

RECENT_DAYS = 90
# name, unit, lowest, highest, decimals
METRICS: dict[str, tuple[str, str, int, int, int]] = {
    WEIGHT_KG: ("Weight", "kg", 20, 250, 1),
    RESTING_HR: ("Resting heart rate", "bpm", 20, 150, 0),
    FTP_W: ("FTP", "W", FTP_MIN, FTP_MAX, 0),
    MAX_HR: ("Max heart rate", "bpm", 80, 250, 0),
}


def _table(columns: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.horizontalHeader().setDefaultAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(32)
    table.setShowGrid(False)
    return table


def _daily_loads(rides: list[SessionRow], today: date) -> tuple[list[date], list[float]]:
    """Training load per local day from the first ride to today, rest days as 0."""
    per_day: dict[date, float] = {}
    for s in rides:
        day = s.started_at_utc.astimezone().date()
        per_day[day] = per_day.get(day, 0.0) + (s.load or 0.0)
    if not per_day:
        return [], []
    first = min(per_day)
    days = [first + timedelta(days=i) for i in range((max(today, first) - first).days + 1)]
    return days, [per_day.get(d, 0.0) for d in days]


def _weekly(rides: list[SessionRow], today: date) -> tuple[list[date], list[float], list[float]]:
    """Load and hours per week (Mondays) from the first ride to the current week."""
    this_week = today - timedelta(days=today.weekday())
    oldest = min((s.started_at_utc.astimezone().date() for s in rides), default=today)
    count = (this_week - (oldest - timedelta(days=oldest.weekday()))).days // 7 + 1
    weeks = [this_week - timedelta(weeks=i) for i in range(count - 1, -1, -1)]
    load = dict.fromkeys(weeks, 0.0)
    hours = dict.fromkeys(weeks, 0.0)
    for s in rides:
        day = s.started_at_utc.astimezone().date()
        monday = day - timedelta(days=day.weekday())
        if monday in load:
            load[monday] += s.load or 0.0
            hours[monday] += (s.duration_s or 0.0) / 3600
    first = next((i for i, w in enumerate(weeks) if load[w] or hours[w]), None)
    if first is None:
        return [], [], []
    weeks = weeks[first:]
    return weeks, [load[w] for w in weeks], [hours[w] for w in weeks]


class ProgressPage(QWidget):
    metrics_changed = pyqtSignal()

    def __init__(self, repo: Repo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._entries: list[Metric] = []
        self._imperial = False
        self._guessed: dict[int, float | None] = {}  # session id -> estimated load

        self.volume = text_label("body")
        self.peaks = _table(["Duration", "All time", "W/kg", "Date", f"Last {RECENT_DAYS} days"])
        dates = QHeaderView.ResizeMode.ResizeToContents
        self.peaks.horizontalHeader().setSectionResizeMode(3, dates)
        self.peaks.horizontalHeader().setSectionResizeMode(4, dates)
        best = QGroupBox("Best power")
        best_layout = QVBoxLayout(best)
        best_layout.addWidget(self.peaks)

        self.metric = QComboBox()
        for key, (text, *_rest) in METRICS.items():
            self.metric.addItem(text, key)
        self.day = QDateEdit(QDate.currentDate())
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("yyyy-MM-dd")
        self.value = QDoubleSpinBox()
        self.add_button = QPushButton("Add")
        self.delete_button = QPushButton("Delete entry")
        theme.set_role(self.delete_button, "danger")
        theme.set_role(self.add_button, "primary")
        self.log = _table(["Date", "Metric", "Value"])
        entry = QHBoxLayout()
        for widget in (self.metric, self.day, self.value, self.add_button):
            entry.addWidget(widget)
        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        log_layout.addLayout(entry)
        log_layout.addWidget(self.log, 1)
        log_layout.addWidget(self.delete_button)

        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setStyleSheet(f"color: {theme.ERROR}")
        layout = QVBoxLayout(self)
        page_layout(self, layout)
        self.best_button = QPushButton("Best power")
        self.log_button = QPushButton("Log")
        top = QHBoxLayout()
        top.setSpacing(GAP)
        top.addWidget(text_label("title", "Progress"))
        top.addWidget(self.message, 1)
        for button in (self.best_button, self.log_button):
            button.setCheckable(True)
            top.addWidget(button)
        layout.addLayout(top)
        layout.addWidget(card(self.volume, margin=14))
        # Panels float over the right side of the chart, so the chart never changes size.
        self._panels = {
            self.best_button: card(best, margin=6),
            self.log_button: card(log_box, margin=6),
        }
        for panel in self._panels.values():
            panel.setParent(self)
            panel.hide()

        self.curve = PowerCurvePlot(RECENT_DAYS)
        self.form = FormPlot()
        self.weekly = WeeklyPlot()
        self.trend = TrendPlot()
        self.trend_metric = QComboBox()
        for key, (text, *_rest) in METRICS.items():
            self.trend_metric.addItem(text, key)
        self.trend_metric.setCurrentIndex(list(METRICS).index(FTP_W))
        trend = QWidget()
        trend_layout = QVBoxLayout(trend)
        trend_layout.setContentsMargins(0, 0, 0, 0)
        trend_layout.addWidget(self.trend_metric)
        trend_layout.addWidget(self.trend, 1)
        self.export_button = QPushButton("Export plot…")
        self.charts = QTabWidget()
        self.charts.setCornerWidget(self.export_button)
        self.charts.addTab(self.curve, "Power curve")
        self.charts.addTab(self.form, "Fitness and form")
        self.charts.addTab(self.weekly, "Weekly load")
        self.charts.addTab(trend, "Trends")
        self.charts.setMinimumHeight(340)
        layout.addWidget(self.charts, 1)
        self.charts.installEventFilter(self)
        pad_spin_boxes(self)
        for button in self._panels:
            button.clicked.connect(lambda _c=False, b=button: self._toggle_panel(b))

        self.metric.currentIndexChanged.connect(lambda _i: self._guard(self._prepare_input))
        self.add_button.clicked.connect(lambda: self._guard(self._add))
        self.delete_button.clicked.connect(lambda: self._guard(self._delete))
        self.log.itemSelectionChanged.connect(self._update_buttons)
        self.trend_metric.currentIndexChanged.connect(lambda _i: self._guard(self._load_trend))
        self.export_button.clicked.connect(lambda: self._guard(self.export_plot))

    def refresh(self) -> None:
        """Waits until the page is shown: guessing loads reads all samples of some rides."""
        if self.isVisible():
            self._guard(self._load)

    def _toggle_panel(self, clicked: QPushButton) -> None:
        """Show the clicked panel over the chart, hiding the other; click again to close."""
        for button, panel in self._panels.items():
            on = button is clicked and button.isChecked()
            button.setChecked(on)
            panel.setVisible(on)
        self._place_panels()

    def _place_panels(self) -> None:
        area = self.charts.geometry()
        width = max(min(area.width() * 55 // 100, 720), min(area.width(), 360))
        for panel in self._panels.values():
            panel.setGeometry(area.right() - width + 1, area.top(), width, area.height())
            panel.raise_()

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if (
            obj is self.charts
            and event is not None
            and event.type()
            in (
                QEvent.Type.Resize,
                QEvent.Type.Move,
            )
        ):
            self._place_panels()
        return super().eventFilter(obj, event)

    def confirm_delete(self) -> bool:
        answer = QMessageBox.question(self, "Delete entry", "Delete the selected entry?")
        return answer == QMessageBox.StandardButton.Yes

    def showEvent(self, event: QShowEvent | None) -> None:
        super().showEvent(event)
        self.refresh()

    def _guard(self, action: Callable[[], None]) -> None:
        """Never let a failure escape a slot: PyQt6 would abort the app."""
        self.message.clear()
        try:
            action()
        except Exception as e:
            self.message.setText(f"{type(e).__name__}: {e}")

    def _load(self) -> None:
        self._imperial = self._repo.get_setting(UNITS) == "imperial"
        now = datetime.now(UTC)
        self.day.setMaximumDate(QDate.currentDate())
        best = self._repo.best_peaks()
        recent = self._repo.best_peaks(now - timedelta(days=RECENT_DAYS))
        self.peaks.setRowCount(len(PEAK_DURATIONS))
        for row, seconds in enumerate(PEAK_DURATIONS):
            late = recent.get(seconds)
            cells = [duration_label(seconds), *self._peak_cells(best.get(seconds))]
            cells.append(f"{late.watts:.0f} W" if late else NO_VALUE)
            for col, text in enumerate(cells):
                self.peaks.setItem(row, col, QTableWidgetItem(text))
        profile = load_profile(self._repo)
        self.curve.set_curves(best, recent, profile.ftp_w, profile.weight_kg)
        rides = [self._with_load(s) for s in self._repo.list_sessions() if s.ended_at_utc]
        self.volume.setText("\n".join(self._volume(rides, now, days) for days in (7, 42)))
        days, loads = _daily_loads(rides, now.astimezone().date())
        self.form.set_series(days, fitness_form(loads))
        weeks, week_load, week_hours = _weekly(rides, now.astimezone().date())
        self.weekly.set_weeks(weeks, week_load, week_hours)
        self._entries = self._repo.list_metrics()
        self._load_trend()
        self.log.setRowCount(len(self._entries))
        for row, e in enumerate(self._entries):
            name = METRICS[e.metric][0] if e.metric in METRICS else e.metric
            for col, text in enumerate((e.day.isoformat(), name, self._format(e))):
                self.log.setItem(row, col, QTableWidgetItem(text))
        self._prepare_input()
        self._update_buttons()

    def _with_load(self, ride: SessionRow) -> SessionRow:
        """Rides without power get a guessed load from speed, altitude and length.
        The guess is only used here, never stored."""
        if ride.load is not None or ride.avg_power is not None or ride.avg_hr is not None:
            return ride
        if ride.id not in self._guessed:
            samples = self._repo.speed_altitude(ride.id)
            self._guessed[ride.id] = estimated_load(
                [speed for speed, _altitude in samples],
                [altitude for _speed, altitude in samples],
                ride.weight_kg,
                ride.ftp_w,
            )
        return replace(ride, load=self._guessed[ride.id])

    def _load_trend(self) -> None:
        key = self.trend_metric.currentData()
        name, unit, *_rest = METRICS[key]
        entries = sorted((e for e in self._entries if e.metric == key), key=lambda e: e.day)
        factor = LB_PER_KG if self._imperial and key == WEIGHT_KG else 1.0
        if factor != 1:
            unit = "lb"
        days = [e.day for e in entries]
        self.trend.set_series(name, unit, days, [e.value * factor for e in entries])

    def ask_export_path(self, suggestion: str) -> Path | None:
        path, _filter = QFileDialog.getSaveFileName(self, "Export plot", suggestion, "PNG (*.png)")
        return Path(path) if path else None

    def export_plot(self) -> None:
        """Save the shown chart, with its labels and summary, as an image."""
        widget = self.charts.currentWidget()
        name = self.charts.tabText(self.charts.currentIndex()).lower().replace(" ", "-")
        path = self.ask_export_path(f"pywatts-{name}.png")
        if path is None or widget is None:
            return
        if not widget.grab().save(str(path), "PNG"):
            raise OSError(f"could not write {path}")
        self.message.setText(f"Saved {path.name}")

    @staticmethod
    def _peak_cells(peak: Peak | None) -> list[str]:
        if peak is None:
            return [NO_VALUE] * 3
        ratio = f"{peak.watts / peak.weight_kg:.2f}" if peak.weight_kg else NO_VALUE
        return [f"{peak.watts:.0f} W", ratio, f"{peak.started_at_utc.astimezone():%Y-%m-%d}"]

    @staticmethod
    def _volume(all_rides: list[SessionRow], now: datetime, days: int) -> str:
        rides = [s for s in all_rides if s.started_at_utc >= now - timedelta(days=days)]
        minutes = int(sum(s.duration_s or 0 for s in rides) // 60)
        return (
            f"Last {days} days:  {len(rides)} rides  ·  {minutes // 60}:{minutes % 60:02d} h  ·  "
            f"{sum(s.kj or 0 for s in rides):.0f} kJ  ·  "
            f"training load {sum(s.load or 0 for s in rides):.0f}"
        )

    def _format(self, e: Metric) -> str:
        if e.metric == WEIGHT_KG:
            return f"{e.value * LB_PER_KG:.1f} lb" if self._imperial else f"{e.value:.1f} kg"
        return f"{e.value:.0f} {METRICS[e.metric][1] if e.metric in METRICS else ''}".strip()

    def _factor(self) -> float:
        return LB_PER_KG if self._imperial and self.metric.currentData() == WEIGHT_KG else 1.0

    def _prepare_input(self) -> None:
        """Limits and unit of the chosen metric, starting at the current value."""
        key = self.metric.currentData()
        _name, unit, low, high, digits = METRICS[key]
        factor = self._factor()
        self.value.setDecimals(digits)
        self.value.setRange(low * factor, high * factor)
        self.value.setSuffix(f" {'lb' if factor != 1 else unit}")
        self.value.setValue(getattr(load_profile(self._repo), key) * factor)

    def _add(self) -> None:
        key = self.metric.currentData()
        value = round(self.value.value() / self._factor(), 2)
        self._repo.record_metric(key, value, self.day.date().toPyDate())
        self._load()
        self.metrics_changed.emit()

    def _delete(self) -> None:
        row = self.log.currentRow()
        if self.log.selectedItems() and self.confirm_delete():
            self._repo.delete_metric(self._entries[row].id)
            self._load()
            self.metrics_changed.emit()

    def _update_buttons(self) -> None:
        self.delete_button.setEnabled(bool(self.log.selectedItems()))
