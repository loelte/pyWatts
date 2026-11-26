"""Best power curve (all time and recent), fitness / fatigue / form, load and trend charts.

Every chart takes its colors (theme.SCREEN_CHART or theme.PRINT_CHART) and can make a hidden
copy of itself in other colors: the export draws that copy, never the dark screen."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QActionGroup, QFont, QFontMetrics
from PyQt6.QtWidgets import QButtonGroup, QHBoxLayout, QMenu, QPushButton, QVBoxLayout, QWidget

from pywatts.core.metrics import estimated_ftp, gaussian_smooth
from pywatts.storage.repo import Peak
from pywatts.ui import theme
from pywatts.ui.theme import ChartColors
from pywatts.ui.widgets.common import text_label

NO_VALUE = "--"
TICK_SECONDS = (5, 30, 60, 300, 600, 1200, 3600)


def duration_label(seconds: int) -> str:
    return f"{seconds} s" if seconds < 60 else f"{seconds // 60} min"


def _font(colors: ChartColors) -> QFont | None:
    if colors.font_px is None:
        return None
    font = QFont()
    font.setPixelSize(colors.font_px)
    return font


def _style(plot: pg.PlotWidget, colors: ChartColors) -> None:
    plot.setMenuEnabled(False)
    plot.hideButtons()
    plot.setMouseEnabled(x=False, y=False)
    plot.setBackground(colors.background)
    font = _font(colors)
    for name in ("left", "bottom"):
        axis = plot.getAxis(name)
        axis.enableAutoSIPrefix(False)
        axis.setPen(colors.axis)
        axis.setTextPen(colors.text)
        axis.labelStyle["color"] = colors.text
        if font is not None:
            axis.setTickFont(font)
            # pyqtgraph makes room for larger text only after painting once: too late for
            # a single export. setStyle applies the size.
            axis.textHeight = QFontMetrics(font).height() + 2
            axis.setStyle(autoExpandTextSpace=True)
            axis.labelStyle["font-size"] = f"{colors.font_px}px"
    plot.showGrid(x=True, y=True, alpha=colors.grid_alpha)


def _line(colors: ChartColors, color: str, dots: bool) -> dict:
    """Options of PlotWidget.plot for a solid line, with dots on the values if asked."""
    options: dict = {"pen": pg.mkPen(color, width=colors.line_width)}
    if dots:
        options.update(symbol="o", symbolSize=colors.symbol_size, symbolBrush=color, symbolPen=None)
    return options


def _level(colors: ChartColors, color: str, label: str, position: float) -> pg.InfiniteLine:
    """A dashed horizontal line with a label at one end, unless a legend explains it."""
    line = pg.InfiniteLine(
        angle=0,
        pen=pg.mkPen(color, width=max(1, colors.line_width - 1), style=Qt.PenStyle.DashLine),
        label=label,
        labelOpts={"position": position, "color": color},
    )
    if not colors.line_labels:
        line.label.hide()
    return line


@dataclass(frozen=True)
class LegendEntry:
    color: str
    text: str
    mark: Literal["line", "dots", "dash", "bar"]


class Chart(QWidget):
    """A progress chart that can be drawn again in other colors, for the export."""

    plot: pg.PlotWidget

    def __init__(self, colors: ChartColors, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.colors = colors
        self.export_summary = ""  # the summary without hints about using the screen

    def clone(self, colors: ChartColors) -> Chart:
        """A hidden copy with the same data, options and time span in other colors."""
        raise NotImplementedError

    def title(self) -> str:
        raise NotImplementedError

    def subtitle(self) -> str:
        raise NotImplementedError

    def legend_entries(self) -> list[LegendEntry]:
        raise NotImplementedError


class PowerCurvePlot(Chart):
    """Mean-maximal power over duration on a log axis. Hover shows both curves at a duration."""

    def __init__(
        self,
        recent_days: int,
        parent: QWidget | None = None,
        *,
        colors: ChartColors = theme.SCREEN_CHART,
    ) -> None:
        super().__init__(colors, parent)
        self._recent_days = recent_days
        self._best: dict[int, Peak] = {}
        self._recent: dict[int, Peak] = {}
        self._ftp_w = 0
        self._weight_kg: float | None = None
        self.per_kg = False

        self.plot = pg.PlotWidget()
        _style(self.plot, colors)
        self.plot.setLabel("bottom", "duration")
        self.plot.getAxis("bottom").setTicks(
            [[(math.log10(d), duration_label(d)) for d in TICK_SECONDS]]
        )
        self.plot.setMinimumHeight(220)
        self._all_line = self.plot.plot(**_line(colors, colors.first, dots=True))
        self._recent_line = self.plot.plot(**_line(colors, colors.second, dots=True))
        self._ftp_line = _level(colors, colors.reference, "FTP", 0.95)
        self.plot.addItem(self._ftp_line, ignoreBounds=True)
        self._cursor = pg.InfiniteLine(pen=pg.mkPen(colors.zero, width=1))
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

    def clone(self, colors: ChartColors) -> PowerCurvePlot:
        copy = PowerCurvePlot(self._recent_days, colors=colors)
        copy.per_kg = self.per_kg
        copy.set_curves(self._best, self._recent, self._ftp_w, self._weight_kg)
        return copy

    def title(self) -> str:
        return "Power curve"

    def subtitle(self) -> str:
        unit = "W/kg" if self.per_kg else "watts"
        return f"Best average power over each duration, in {unit}"

    def legend_entries(self) -> list[LegendEntry]:
        entries = [
            LegendEntry(self.colors.first, "All time", "dots"),
            LegendEntry(self.colors.second, f"Last {self._recent_days} days", "dots"),
        ]
        if self._ftp_line.isVisible():
            entries.append(LegendEntry(self.colors.reference, "FTP", "dash"))
        return entries

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
        self.export_summary = self._insight()
        self.summary.setText(self.export_summary)

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
SMOOTH_DAYS = 2  # sigma of the Gaussian over the form chart
RANGE_HINT = "Right click the chart to change the time span."
DAY_S = 86400


def span_text(days: int | None) -> str:
    """'last 6 months', 'last month', 'all time'."""
    if days is None:
        return "all time"
    text = next((t for t, d in RANGES if d == days), f"{days} days")
    return f"last {text.removeprefix('1 ')}"


class RangePlot(Chart):
    """A chart over dates. Right click chooses the time span, which ends today."""

    def __init__(
        self,
        range_days: int | None,
        parent: QWidget | None = None,
        *,
        colors: ChartColors = theme.SCREEN_CHART,
    ) -> None:
        super().__init__(colors, parent)
        self.range_days = range_days
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        _style(self.plot, colors)
        self.plot.setMinimumHeight(220)
        self.plot.scene().sigMouseClicked.connect(self._on_click)  # type: ignore[union-attr]
        self.summary = text_label("body")
        self.summary.setWordWrap(True)

    def set_range(self, days: int | None) -> None:
        """Days shown ending today; None = everything."""
        self.range_days = days
        self._redraw()

    def _set_summary(self, text: str, hint: bool = True) -> None:
        self.export_summary = text
        self.summary.setText(f"{text}\n{RANGE_HINT}" if hint else text)

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
        raise NotImplementedError


class FormPlot(RangePlot):
    """Fitness (long average of the training load), fatigue (short) and form (difference)."""

    def __init__(
        self, parent: QWidget | None = None, *, colors: ChartColors = theme.SCREEN_CHART
    ) -> None:
        super().__init__(DEFAULT_RANGE_DAYS, parent, colors=colors)
        self.plot.setLabel("left", "load / day")
        self.plot.addItem(
            pg.InfiniteLine(angle=0, pen=pg.mkPen(colors.zero, width=1)), ignoreBounds=True
        )
        self._lines = {
            "fitness": self.plot.plot(**_line(colors, colors.first, dots=False)),
            "fatigue": self.plot.plot(**_line(colors, colors.second, dots=False)),
            "form": self.plot.plot(**_line(colors, colors.third, dots=False)),
        }
        self.legend = text_label(
            "muted", "Blue: fitness (42 days)    Amber: fatigue (7 days)    Green: form"
        )
        self._days: list[date] = []
        self._values: list[tuple[float, float, float]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.legend)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.summary)

    def clone(self, colors: ChartColors) -> FormPlot:
        copy = FormPlot(colors=colors)
        copy.range_days = self.range_days
        copy.set_series(self._days, self._values)
        return copy

    def title(self) -> str:
        return "Fitness and form"

    def subtitle(self) -> str:
        return f"From the training load per day, {span_text(self.range_days)}"

    def legend_entries(self) -> list[LegendEntry]:
        return [
            LegendEntry(self.colors.first, "Fitness (42 days)", "line"),
            LegendEntry(self.colors.second, "Fatigue (7 days)", "line"),
            LegendEntry(self.colors.third, "Form (fitness - fatigue)", "line"),
        ]

    def set_series(self, days: list[date], values: list[tuple[float, float, float]]) -> None:
        """values: (fitness, fatigue, form) per day, same length as days: the whole history."""
        self._days, self._values = days, values
        self._redraw()

    def _redraw(self) -> None:
        days, values = self._days, self._values
        if not days:
            for line in self._lines.values():
                line.setData([], [])
            self._set_summary("No rides yet.", hint=False)
            return
        xs = [datetime.combine(d, time(12)).timestamp() for d in days]
        columns = [gaussian_smooth([v[i] for v in values], SMOOTH_DAYS) for i in range(3)]
        for line, column in zip(self._lines.values(), columns, strict=True):
            line.setData(xs, column)
        fitness, fatigue, form = values[-1]  # today's numbers stay unsmoothed
        first = 0 if self.range_days is None else max(0, len(days) - self.range_days)
        self.plot.setXRange(xs[first], xs[-1] + DAY_S, padding=0)
        low = min(column[i] for column in columns[2:] for i in range(first, len(days)))
        high = max(column[i] for column in columns[:2] for i in range(first, len(days)))
        self.plot.setYRange(min(low, 0) * 1.1, max(high, 1) * 1.1, padding=0)
        self._set_summary(
            f"Today: fitness {fitness:.0f}  ·  fatigue {fatigue:.0f}  ·  form {form:+.0f} "
            f"({form_state(form)})"
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


@dataclass(frozen=True)
class Period:
    button: str
    days: float  # mean length
    average_of: int  # periods before the current one
    current: str  # summary heading
    previous: str  # plural, for "the average of the 4 weeks before"


PERIODS: dict[str, Period] = {
    "day": Period("Day", 1, 7, "Today", "days"),
    "week": Period("Week", 7, 4, "This week", "weeks"),
    "month": Period("Month", 365.25 / 12, 3, "This month", "months"),
}
MEASURES: dict[str, str] = {"load": "Load", "hours": "Hours", "distance": "Distance"}
MOVING_SIGMA_DAYS = 7  # Gaussian over the moving average of the load chart


def period_start(day: date, period: str) -> date:
    if period == "day":
        return day
    if period == "week":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def period_end(start: date, period: str) -> date:
    """First day of the next period."""
    if period == "day":
        return start + timedelta(days=1)
    if period == "week":
        return start + timedelta(weeks=1)
    return date(start.year + start.month // 12, start.month % 12 + 1, 1)


def by_period(days: list[date], values: list[float], period: str) -> tuple[list[date], list[float]]:
    """Sums of consecutive days per period; returns the first day of each period."""
    starts: list[date] = []
    sums: list[float] = []
    for day, value in zip(days, values, strict=True):
        start = period_start(day, period)
        if not starts or starts[-1] != start:
            starts.append(start)
            sums.append(0.0)
        sums[-1] += value
    return starts, sums


class LoadPlot(RangePlot):
    """Training load, hours or distance per day, week (Monday to Sunday) or month as bars."""

    def __init__(
        self, parent: QWidget | None = None, *, colors: ChartColors = theme.SCREEN_CHART
    ) -> None:
        super().__init__(DEFAULT_RANGE_DAYS, parent, colors=colors)
        self.period = "week"
        self.measure = "load"
        self._days: list[date] = []
        self._series: dict[str, list[float]] = {key: [] for key in MEASURES}
        self._distance_unit = "km"
        self._bars = pg.BarGraphItem(x0=[], x1=[], height=[], brush=colors.first, pen=None)
        self.plot.addItem(self._bars)
        self._moving = self.plot.plot(**_line(colors, colors.third, dots=False))
        self._average = _level(colors, colors.second, "average", 0.1)
        self._average.hide()
        self.plot.addItem(self._average, ignoreBounds=True)
        self.period_buttons = _choice_buttons(
            self, {k: p.button for k, p in PERIODS.items()}, self.period, self.set_period
        )
        self.measure_buttons = _choice_buttons(self, MEASURES, self.measure, self.set_measure)
        self.legend = text_label("muted")
        bar = QHBoxLayout()
        for button in self.period_buttons.values():
            bar.addWidget(button)
        bar.addSpacing(12)
        bar.addWidget(self.legend, 1)
        for button in self.measure_buttons.values():
            bar.addWidget(button)
        bar.addSpacing(12)
        self.fft_button = QPushButton("FFT")  # the page opens the window
        self.fft_button.setToolTip("Rhythms in the shown measure, over every day")
        bar.addWidget(self.fft_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.summary)

    def daily(self) -> tuple[str, str, list[float]]:
        """Name, unit and the total per day of the shown measure, the whole history."""
        return MEASURES[self.measure], self._unit(), self._series[self.measure]

    def clone(self, colors: ChartColors) -> LoadPlot:
        copy = LoadPlot(colors=colors)
        copy.range_days, copy.period, copy.measure = self.range_days, self.period, self.measure
        series = self._series
        copy.set_days(
            self._days, series["load"], series["hours"], series["distance"], self._distance_unit
        )
        return copy

    def title(self) -> str:
        return f"{MEASURES[self.measure]} per {self.period}"

    def subtitle(self) -> str:
        what = {
            "load": "Training load",
            "hours": "Riding time in hours",
            "distance": f"Distance in {self._distance_unit}",
        }[self.measure]
        weeks = ", Monday to Sunday" if self.period == "week" else ""
        return f"{what} per {self.period}{weeks}, {span_text(self.range_days)}"

    def legend_entries(self) -> list[LegendEntry]:
        entries = [LegendEntry(self.colors.first, self.title(), "bar")]
        if self.moving_average():
            entries.append(LegendEntry(self.colors.third, self._moving_name(), "line"))
        if self._average.isVisible():
            entries.append(LegendEntry(self.colors.second, self._average_name(), "dash"))
        return entries

    def _moving_name(self) -> str:
        """'4-week moving average'"""
        return f"{PERIODS[self.period].average_of}-{self.period} moving average"

    def _average_name(self) -> str:
        """'Last 4-week average load', of full periods: the current one is still running."""
        period = PERIODS[self.period]
        measure = MEASURES[self.measure].lower()
        return f"Last {period.average_of}-{self.period} average {measure}"

    def set_days(
        self,
        days: list[date],
        loads: list[float],
        hours: list[float],
        distances: list[float],
        distance_unit: str = "km",
    ) -> None:
        """Totals per day, oldest first, the whole history up to today, rest days as 0.
        distances are in distance_unit."""
        self._days = days
        self._series = {"load": loads, "hours": hours, "distance": distances}
        self._distance_unit = distance_unit
        self._redraw()

    def set_period(self, period: str) -> None:
        self.period = period
        for key, button in self.period_buttons.items():
            button.setChecked(key == period)
        self._redraw()

    def set_measure(self, measure: str) -> None:
        self.measure = measure
        for key, button in self.measure_buttons.items():
            button.setChecked(key == measure)
        self._redraw()

    def bars(self, measure: str | None = None) -> tuple[list[date], list[float]]:
        """First day of each period and its total, oldest first; the last is the current one."""
        return by_period(self._days, self._series[measure or self.measure], self.period)

    def moving_average(self) -> list[float]:
        """The green line: moving average of the shown bars from the first full window on,
        smoothed with a Gaussian of one week. Only full periods count, as for the dashed
        line; the current one is still running."""
        period = PERIODS[self.period]
        moving = moving_average(self.bars()[1][:-1], period.average_of)
        return gaussian_smooth(moving, MOVING_SIGMA_DAYS / period.days) if moving else []

    def _unit(self) -> str:
        return {"load": "load", "hours": "h", "distance": self._distance_unit}[self.measure]

    def _digits(self) -> int:
        return 1 if self.measure == "hours" else 0

    def _redraw(self) -> None:
        period = PERIODS[self.period]
        starts, values = self.bars()
        ends = [period_end(s, self.period) for s in starts]
        x0 = [datetime.combine(s, time()).timestamp() for s in starts]
        x1 = [datetime.combine(e, time()).timestamp() for e in ends]
        gap = period.days * DAY_S / 7
        self._bars.setOpts(x0=[x + gap for x in x0], x1=[x - gap for x in x1], height=values)
        self.plot.setLabel("left", f"{self._unit()} / {self.period}")
        moving = self.moving_average()
        middles = [(a + b) / 2 for a, b in zip(x0, x1, strict=True)]
        self._moving.setData(middles[period.average_of - 1 :][: len(moving)], moving)
        if not starts:
            self._average.hide()
            self.legend.clear()
            self._set_summary("No rides yet.", hint=False)
            return
        count = len(starts) if self.range_days is None else math.ceil(self.range_days / period.days)
        first = max(0, len(starts) - count)
        pad = gap / 2
        self.plot.setXRange(x0[first] - pad, x1[-1] + pad, padding=0)
        self.plot.setYRange(0, max([*values[first:], 1]) * 1.15, padding=0)
        previous = values[-period.average_of - 1 : -1]
        average = average_of(previous)
        self._average.setValue(average)
        self._average.label.setFormat(
            f"{self._average_name().lower()}: {average:.{self._digits()}f}"
        )
        self._average.setVisible(bool(previous))
        parts = []
        if moving:
            parts.append(f"Green: {self._moving_name()}")
        if previous:
            parts.append(f"Dashed: {self._average_name().lower()}")
        self.legend.setText("    ".join(parts))
        totals = {key: self.bars(key)[1][-1] for key in MEASURES}
        text = (
            f"{period.current}: load {totals['load']:.0f}, {totals['hours']:.1f} h, "
            f"{totals['distance']:.0f} {self._distance_unit}"
        )
        if previous and average > 0:
            text += (
                f"  ·  {MEASURES[self.measure].lower()} {100 * values[-1] / average:.0f}% of the "
                f"average of the {period.average_of} {period.previous} before"
            )
        loads = self.bars("load")[1]
        usual = loads[-period.average_of - 1 : -1]
        if self.period == "week" and usual and loads[-2] > 1.3 * average_of(usual):
            text += "\nLast week was a big jump over your usual load: ease in if you feel tired."
        self._set_summary(text)


def average_of(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def moving_average(values: list[float], window: int) -> list[float]:
    """Average of each value and the window - 1 before it, from the first full window on."""
    return [average_of(values[i - window + 1 : i + 1]) for i in range(window - 1, len(values))]


def _choice_buttons(
    parent: QWidget, options: dict[str, str], current: str, pick: Callable[[str], None]
) -> dict[str, QPushButton]:
    """Exclusive, checkable buttons; pick gets the key of the clicked one."""
    group = QButtonGroup(parent)
    group.setExclusive(True)
    buttons = {}
    for key, text in options.items():
        button = QPushButton(text)
        button.setCheckable(True)
        button.setChecked(key == current)
        button.clicked.connect(lambda _c=False, k=key: pick(k))
        group.addButton(button)
        buttons[key] = button
    return buttons


class TrendPlot(RangePlot):
    """One tracked value (FTP, weight, heart rates) over time, as logged."""

    def __init__(
        self, parent: QWidget | None = None, *, colors: ChartColors = theme.SCREEN_CHART
    ) -> None:
        super().__init__(None, parent, colors=colors)
        self._name = self._unit = ""
        self._days: list[date] = []
        self._values: list[float] = []
        self._line = self.plot.plot(**_line(colors, colors.first, dots=True))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.summary)

    def clone(self, colors: ChartColors) -> TrendPlot:
        copy = TrendPlot(colors=colors)
        copy.range_days = self.range_days
        copy.set_series(self._name, self._unit, self._days, self._values)
        return copy

    def title(self) -> str:
        return self._name or "Trend"

    def subtitle(self) -> str:
        return f"As logged, in {self._unit}, {span_text(self.range_days)}"

    def legend_entries(self) -> list[LegendEntry]:
        return [LegendEntry(self.colors.first, self._name, "dots")] if self._name else []

    def set_series(self, name: str, unit: str, days: list[date], values: list[float]) -> None:
        """Oldest first, the whole history."""
        self._name, self._unit, self._days, self._values = name, unit, days, values
        self._redraw()

    def _redraw(self) -> None:
        name, unit, days, values = self._name, self._unit, self._days, self._values
        if not name:
            return
        self.plot.setLabel("left", f"{name} ({unit})")
        xs = [datetime.combine(d, time(12)).timestamp() for d in days]
        self._line.setData(xs, values)
        if not days:
            self._set_summary(f"No {name.lower()} logged yet.", hint=False)
            return
        now = datetime.now().timestamp()
        start = xs[0]
        if self.range_days is not None:
            start = max(start, now - self.range_days * DAY_S)
        first = next((i for i, x in enumerate(xs) if x >= start), len(xs))
        visible = values[first:]
        if 0 < first < len(xs):  # the line enters from an older entry
            share = (start - xs[first - 1]) / (xs[first] - xs[first - 1])
            visible = [values[first - 1] + share * (values[first] - values[first - 1]), *visible]
        low, high = min(visible or values[-1:]), max(visible or values[-1:])
        pad = max((high - low) * 0.2, high * 0.02, 1)
        self.plot.setYRange(low - pad, high + pad, padding=0)
        span = DAY_S * 3
        self.plot.setXRange(start - span, max(xs[-1], now) + span, padding=0)
        text = f"{values[-1]:g} {unit} on {days[-1]:%Y-%m-%d}  ·  "
        if first < len(xs):
            text += f"{values[-1] - values[first]:+.1f} {unit} since {days[first]:%Y-%m-%d}"
        else:
            text += "nothing logged in this time span"
        self._set_summary(text)
