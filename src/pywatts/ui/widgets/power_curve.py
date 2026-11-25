"""Best power curve (all time and recent) and the fitness / fatigue / form chart."""

from __future__ import annotations

import math
from datetime import date, datetime, time

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QActionGroup
from PyQt6.QtWidgets import QButtonGroup, QHBoxLayout, QMenu, QPushButton, QVBoxLayout, QWidget

from pywatts.core.metrics import estimated_ftp, smooth
from pywatts.storage.repo import Peak
from pywatts.ui import theme
from pywatts.ui.widgets.common import text_label

NO_VALUE = "--"
TICK_SECONDS = (5, 30, 60, 300, 600, 1200, 3600)


def duration_label(seconds: int) -> str:
    return f"{seconds} s" if seconds < 60 else f"{seconds // 60} min"


def _style(plot: pg.PlotWidget) -> None:
    plot.setMenuEnabled(False)
    plot.hideButtons()
    plot.setMouseEnabled(x=False, y=False)
    plot.showGrid(x=True, y=True, alpha=0.12)
    for axis in ("left", "bottom"):
        plot.getAxis(axis).enableAutoSIPrefix(False)


class PowerCurvePlot(QWidget):
    """Mean-maximal power over duration on a log axis. Hover shows both curves at a duration."""

    def __init__(self, recent_days: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._recent_days = recent_days
        self._best: dict[int, Peak] = {}
        self._recent: dict[int, Peak] = {}
        self._ftp_w = 0
        self._weight_kg: float | None = None
        self.per_kg = False

        self.plot = pg.PlotWidget()
        _style(self.plot)
        self.plot.setLabel("bottom", "duration")
        self.plot.getAxis("bottom").setTicks(
            [[(math.log10(d), duration_label(d)) for d in TICK_SECONDS]]
        )
        self.plot.setMinimumHeight(220)
        self._all_line = self.plot.plot(
            pen=pg.mkPen(theme.ACCENT, width=2),
            symbol="o",
            symbolSize=6,
            symbolBrush=theme.ACCENT,
            symbolPen=None,
        )
        self._recent_line = self.plot.plot(
            pen=pg.mkPen(theme.POWER, width=2),
            symbol="o",
            symbolSize=6,
            symbolBrush=theme.POWER,
            symbolPen=None,
        )
        self._ftp_line = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen(theme.MUTED, width=1, style=pg.QtCore.Qt.PenStyle.DashLine),
            label="FTP",
            labelOpts={"position": 0.95, "color": theme.MUTED},
        )
        self.plot.addItem(self._ftp_line, ignoreBounds=True)
        self._cursor = pg.InfiniteLine(pen=pg.mkPen(theme.BORDER_STRONG, width=1))
        self._cursor.hide()
        self.plot.addItem(self._cursor, ignoreBounds=True)
        self.plot.scene().sigMouseMoved.connect(self._hover)  # type: ignore[union-attr]

        self.watts_button = QPushButton("W")
        self.kg_button = QPushButton("W/kg")
        group = QButtonGroup(self)
        group.setExclusive(True)
        for button in (self.watts_button, self.kg_button):
            button.setCheckable(True)
            group.addButton(button)
        self.watts_button.setChecked(True)
        self.watts_button.clicked.connect(lambda: self.set_per_kg(False))
        self.kg_button.clicked.connect(lambda: self.set_per_kg(True))

        self.legend = text_label(
            "muted", f"Blue: all time    Amber: last {recent_days} days    Dashed: FTP"
        )
        self.summary = text_label("body")
        self.summary.setWordWrap(True)
        self.readout = text_label("muted", "Move the mouse over the curve.")

        bar = QHBoxLayout()
        bar.addWidget(self.legend, 1)
        bar.addWidget(self.watts_button)
        bar.addWidget(self.kg_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.readout)
        layout.addWidget(self.summary)

    def set_curves(
        self,
        best: dict[int, Peak],
        recent: dict[int, Peak],
        ftp_w: int,
        weight_kg: float | None,
    ) -> None:
        self._best, self._recent = best, recent
        self._ftp_w, self._weight_kg = ftp_w, weight_kg
        self._redraw()
        self.summary.setText(self._insight())

    def set_per_kg(self, per_kg: bool) -> None:
        self.per_kg = per_kg
        self.kg_button.setChecked(per_kg)
        self.watts_button.setChecked(not per_kg)
        self._redraw()

    def _value(self, peak: Peak) -> float | None:
        if not self.per_kg:
            return peak.watts
        return peak.watts / peak.weight_kg if peak.weight_kg else None

    def _points(self, peaks: dict[int, Peak]) -> tuple[list[float], list[float]]:
        xs, ys = [], []
        for seconds in sorted(peaks):
            value = self._value(peaks[seconds])
            if value is not None:
                xs.append(math.log10(seconds))
                ys.append(value)
        return xs, ys

    def _redraw(self) -> None:
        self._all_line.setData(*self._points(self._best))
        self._recent_line.setData(*self._points(self._recent))
        self.plot.setLabel("left", "W/kg" if self.per_kg else "W")
        ftp = self._ftp_w
        if self.per_kg:
            ftp = ftp / self._weight_kg if self._weight_kg else 0
        self._ftp_line.setValue(ftp)
        self._ftp_line.setVisible(ftp > 0)
        top = max([ftp, *self._points(self._best)[1], *self._points(self._recent)[1]])
        self.plot.setYRange(0, max(top, 1) * 1.1, padding=0)
        shortest = min([*self._best, *self._recent, 3])
        longest = max([*self._best, *self._recent, 3600])
        self.plot.setXRange(math.log10(shortest) - 0.05, math.log10(longest) + 0.05, padding=0)

    def _insight(self) -> str:
        """Compare the recent form with the best ever and the 20 min test estimate with the FTP."""
        lines = []
        ratios = [
            self._recent[d].watts / self._best[d].watts
            for d in self._recent
            if d in self._best and self._best[d].watts > 0
        ]
        if ratios:
            lines.append(
                f"Last {self._recent_days} days: {100 * sum(ratios) / len(ratios):.0f}% of your "
                "all-time best on average over all durations."
            )
        twenty = self._recent.get(1200) or self._best.get(1200)
        if twenty is not None and self._ftp_w > 0:
            est = estimated_ftp(twenty.watts)
            diff = est - self._ftp_w
            tail = (
                "close to your FTP"
                if abs(diff) < self._ftp_w * 0.03
                else f"{abs(diff):.0f} W {'above' if diff > 0 else 'below'} your FTP of "
                f"{self._ftp_w} W"
            )
            lines.append(f"95% of your best 20 min is {est:.0f} W, {tail}.")
        return "\n".join(lines)

    def readout_at(self, seconds: int) -> str:
        parts = [duration_label(seconds)]
        for name, peaks in (("all time", self._best), (f"{self._recent_days} days", self._recent)):
            peak = peaks.get(seconds)
            value = self._value(peak) if peak else None
            text = (
                NO_VALUE
                if value is None
                else f"{value:.2f} W/kg"
                if self.per_kg
                else f"{value:.0f} W"
            )
            parts.append(f"{name} {text}")
        best, recent = self._best.get(seconds), self._recent.get(seconds)
        if best and recent and best.watts > 0:
            parts.append(f"{100 * recent.watts / best.watts:.0f}% of best")
        return "  ·  ".join(parts)

    def show_duration(self, seconds: int | None) -> None:
        if seconds is None:
            self._cursor.hide()
            self.readout.setText("Move the mouse over the curve.")
            return
        self._cursor.setValue(math.log10(seconds))
        self._cursor.show()
        self.readout.setText(self.readout_at(seconds))

    def _hover(self, scene_pos) -> None:  # noqa: ANN001
        vb = self.plot.getPlotItem().vb
        if not vb.sceneBoundingRect().contains(scene_pos):
            return
        x = vb.mapSceneToView(scene_pos).x()
        known = sorted(set(self._best) | set(self._recent))
        if known:
            self.show_duration(min(known, key=lambda d: abs(math.log10(d) - x)))


RANGES: tuple[tuple[str, int | None], ...] = (
    ("1 month", 30),
    ("3 months", 90),
    ("6 months", 180),
    ("1 year", 365),
    ("Everything", None),
)
DEFAULT_RANGE_DAYS = 180
SMOOTH_RADIUS = 3  # days on each side


class FormPlot(QWidget):
    """Fitness (long average of the training load), fatigue (short) and form (difference)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        _style(self.plot)
        self.plot.setMinimumHeight(220)
        self.plot.setLabel("left", "load / day")
        self.plot.addItem(
            pg.InfiniteLine(angle=0, pen=pg.mkPen(theme.BORDER_STRONG, width=1)), ignoreBounds=True
        )
        self._lines = {
            "fitness": self.plot.plot(pen=pg.mkPen(theme.ACCENT, width=2)),
            "fatigue": self.plot.plot(pen=pg.mkPen(theme.POWER, width=2)),
            "form": self.plot.plot(pen=pg.mkPen(theme.OK, width=2)),
        }
        self.legend = text_label(
            "muted", "Blue: fitness (42 days)    Amber: fatigue (7 days)    Green: form"
        )
        self._days: list[date] = []
        self._values: list[tuple[float, float, float]] = []
        self.range_days: int | None = DEFAULT_RANGE_DAYS
        self.plot.scene().sigMouseClicked.connect(self._on_click)  # type: ignore[union-attr]
        self.summary = text_label("body")
        self.summary.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.legend)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.summary)

    def set_series(self, days: list[date], values: list[tuple[float, float, float]]) -> None:
        """values: (fitness, fatigue, form) per day, same length as days: the whole history.
        The shown part is `range_days` long; right click changes it."""
        self._days, self._values = days, values
        self._redraw()

    def set_range(self, days: int | None) -> None:
        """Days shown ending today; None = everything."""
        self.range_days = days
        self._redraw()

    def _on_click(self, event) -> None:  # noqa: ANN001
        if event.button() != Qt.MouseButton.RightButton:
            return
        if not self.plot.getPlotItem().vb.sceneBoundingRect().contains(event.scenePos()):
            return
        menu = QMenu(self)
        group = QActionGroup(menu)
        for text, days in RANGES:
            action = menu.addAction(text)
            if action is None:
                continue
            action.setCheckable(True)
            action.setChecked(days == self.range_days)
            action.triggered.connect(lambda _c=False, d=days: self.set_range(d))
            group.addAction(action)
        menu.exec(event.screenPos().toPoint())
        event.accept()

    def _redraw(self) -> None:
        days, values = self._days, self._values
        if not days:
            for line in self._lines.values():
                line.setData([], [])
            self.summary.setText("No rides yet.")
            return
        xs = [datetime.combine(d, time(12)).timestamp() for d in days]
        columns = [smooth([v[i] for v in values], SMOOTH_RADIUS) for i in range(3)]
        for line, column in zip(self._lines.values(), columns, strict=True):
            line.setData(xs, column)
        fitness, fatigue, form = values[-1]  # today's numbers stay unsmoothed
        first = 0 if self.range_days is None else max(0, len(days) - self.range_days)
        self.plot.setXRange(xs[first], xs[-1] + 86400, padding=0)
        low = min(column[i] for column in columns[2:] for i in range(first, len(days)))
        high = max(column[i] for column in columns[:2] for i in range(first, len(days)))
        self.plot.setYRange(min(low, 0) * 1.1, max(high, 1) * 1.1, padding=0)
        self.summary.setText(
            f"Today: fitness {fitness:.0f}  ·  fatigue {fatigue:.0f}  ·  form {form:+.0f} "
            f"({form_state(form)})\nRight click the chart to change the time span."
        )


def form_state(form: float) -> str:
    if form > 15:
        return "very fresh, fitness may be fading"
    if form > 5:
        return "fresh"
    if form > -10:
        return "neutral"
    if form > -30:
        return "building, productive"
    return "heavy fatigue, risk of overreaching"


class WeeklyPlot(QWidget):
    """Training load per week (Monday to Sunday) as bars."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        _style(self.plot)
        self.plot.setMinimumHeight(220)
        self.plot.setLabel("left", "load / week")
        self._bars = pg.BarGraphItem(x=[], height=[], width=86400 * 5, brush=theme.ACCENT, pen=None)
        self.plot.addItem(self._bars)
        self._average = pg.InfiniteLine(
            angle=0,
            pen=pg.mkPen(theme.POWER, width=1, style=pg.QtCore.Qt.PenStyle.DashLine),
            label="4-week average",
            labelOpts={"position": 0.1, "color": theme.POWER},
        )
        self._average.hide()
        self.plot.addItem(self._average, ignoreBounds=True)
        self.summary = text_label("body")
        self.summary.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.summary)

    def set_weeks(self, weeks: list[date], loads: list[float], hours: list[float]) -> None:
        """weeks: Mondays, oldest first; the last one is the current, unfinished week."""
        xs = [datetime.combine(w, time(12)).timestamp() + 86400 * 2 for w in weeks]
        self._bars.setOpts(x=xs, height=loads, width=86400 * 5)
        if not weeks:
            self._average.hide()
            self.summary.setText("No rides yet.")
            return
        self.plot.setXRange(xs[0] - 86400 * 4, xs[-1] + 86400 * 4, padding=0)
        self.plot.setYRange(0, max([*loads, 1]) * 1.15, padding=0)
        previous = loads[-5:-1]
        average = sum(previous) / len(previous) if previous else 0.0
        self._average.setValue(average)
        self._average.setVisible(bool(previous))
        text = f"This week: load {loads[-1]:.0f}, {hours[-1]:.1f} h"
        if previous and average > 0:
            text += f"  ·  {100 * loads[-1] / average:.0f}% of the average of the 4 weeks before"
            if len(loads) >= 2 and loads[-2] > 1.3 * average:
                text += (
                    "\nLast week was a big jump over your usual load: ease in if you feel tired."
                )
        self.summary.setText(text)


class TrendPlot(QWidget):
    """One tracked value (FTP, weight, heart rates) over time, as logged."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        _style(self.plot)
        self.plot.setMinimumHeight(220)
        self._line = self.plot.plot(
            pen=pg.mkPen(theme.ACCENT, width=2),
            symbol="o",
            symbolSize=7,
            symbolBrush=theme.ACCENT,
            symbolPen=None,
        )
        self.summary = text_label("body")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.summary)

    def set_series(self, name: str, unit: str, days: list[date], values: list[float]) -> None:
        """Oldest first."""
        self.plot.setLabel("left", f"{name} ({unit})")
        xs = [datetime.combine(d, time(12)).timestamp() for d in days]
        self._line.setData(xs, values)
        if not days:
            self.summary.setText(f"No {name.lower()} logged yet.")
            return
        low, high = min(values), max(values)
        pad = max((high - low) * 0.2, high * 0.02, 1)
        self.plot.setYRange(low - pad, high + pad, padding=0)
        span = 86400 * 3
        self.plot.setXRange(xs[0] - span, max(xs[-1], datetime.now().timestamp()) + span, padding=0)
        self.summary.setText(
            f"{values[-1]:g} {unit} on {days[-1]:%Y-%m-%d}  ·  "
            f"{values[-1] - values[0]:+.1f} {unit} since {days[0]:%Y-%m-%d}"
        )
