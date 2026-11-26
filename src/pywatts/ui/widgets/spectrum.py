"""A window with the rhythms of the daily load, hours or distance: an FFT over all days."""

from __future__ import annotations

import math

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from pywatts.core.spectrum import Rhythm, Spectrum, rhythms
from pywatts.ui import theme
from pywatts.ui.theme import ChartColors
from pywatts.ui.widgets.common import page_layout, text_label
from pywatts.ui.widgets.power_curve import _line, _style

MONTH_DAYS = 365.25 / 12
TICKS: tuple[tuple[float, str], ...] = (
    (2, "2 d"),
    (3, "3 d"),
    (7, "1 wk"),
    (14, "2 wk"),
    (MONTH_DAYS, "1 mo"),
    (3 * MONTH_DAYS, "3 mo"),
    (6 * MONTH_DAYS, "6 mo"),
    (365.25, "1 yr"),
)
HOVER_HINT = "Move the mouse over the curve."


def period_text(days: float) -> str:
    """'7.0 d', '3.6 wk', '2.1 mo'."""
    if days < 14:
        return f"{days:.1f} d"
    if days < 2 * MONTH_DAYS:
        return f"{days / 7:.1f} wk"
    return f"{days / MONTH_DAYS:.1f} mo"


def _amount(value: float) -> str:
    return f"{value:.2f}" if value < 1 else f"{value:.1f}" if value < 10 else f"{value:.0f}"


class SpectrumWindow(QWidget):
    """Amplitude over period (log axis, days to a year), the strongest rhythms marked."""

    def __init__(
        self, parent: QWidget | None = None, *, colors: ChartColors = theme.SCREEN_CHART
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.colors = colors
        self.resize(900, 560)
        self._unit = ""
        self.spectrum: Spectrum | None = None
        self.plot = pg.PlotWidget()
        _style(self.plot, colors)
        self.plot.setLabel("bottom", "period")
        self.plot.getAxis("bottom").setTicks([[(math.log10(d), text) for d, text in TICKS]])
        self._curve = self.plot.plot(**_line(colors, colors.first, dots=False))
        self._marks: list[pg.InfiniteLine] = []
        self._cursor = pg.InfiniteLine(pen=pg.mkPen(colors.zero, width=1))
        self._cursor.hide()
        self.plot.addItem(self._cursor, ignoreBounds=True)
        self.plot.scene().sigMouseMoved.connect(self._hover)  # type: ignore[union-attr]
        self.heading = text_label("title")
        self.explain = text_label(
            "muted",
            "FFT over every day since the first ride, trend removed. A peak at 1 wk means "
            "the same weekly pattern again and again; longer peaks are blocks or seasons.",
        )
        self.explain.setWordWrap(True)
        self.readout = text_label("muted", HOVER_HINT)
        self.summary = text_label("body")
        self.summary.setWordWrap(True)
        layout = QVBoxLayout(self)
        page_layout(self, layout)
        for widget in (self.heading, self.explain):
            layout.addWidget(widget)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.readout)
        layout.addWidget(self.summary)

    def set_series(self, name: str, unit: str, values: list[float]) -> None:
        """values: one total per day, oldest first, rest days as 0, the whole history."""
        title = f"Rhythms in {name.lower()}"
        self.setWindowTitle(title)
        self.heading.setText(title)
        self._unit = unit
        self.plot.setLabel("left", f"{unit} / day")
        for mark in self._marks:
            self.plot.removeItem(mark)
        self._marks.clear()
        self._cursor.hide()
        self.readout.setText(HOVER_HINT)
        try:
            self.spectrum = rhythms(values)
        except ValueError as e:
            self.spectrum = None
            self._curve.setData([], [])
            self.summary.setText(f"Not enough data yet: {e}.")
            return
        spectrum = self.spectrum
        xs = [math.log10(p) for p in spectrum.periods_days]
        self._curve.setData(xs, spectrum.amplitudes)
        self.plot.setXRange(xs[0], xs[-1], padding=0.02)
        self.plot.setYRange(0, max([*spectrum.amplitudes, 1e-6]) * 1.15, padding=0)
        for rhythm in spectrum.peaks:
            color = self.colors.reference if rhythm.harmonic_of else self.colors.second
            mark = pg.InfiniteLine(
                pos=math.log10(rhythm.period_days),
                angle=90,
                pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine),
                label=period_text(rhythm.period_days),
                labelOpts={"position": 0.95, "color": color},
            )
            self.plot.addItem(mark, ignoreBounds=True)
            self._marks.append(mark)
        self.summary.setText(self._insight(spectrum.peaks))

    def _insight(self, peaks: list[Rhythm]) -> str:
        if not peaks:
            return "No rhythm stands out: the days vary without a repeating pattern."
        parts = []
        for base in (r for r in peaks if r.harmonic_of is None):
            part = (
                f"every {period_text(base.period_days)} "
                f"(±{_amount(base.amplitude)} {self._unit}/day"
            )
            echoes = [r for r in peaks if r.harmonic_of == base.period_days]
            if echoes:
                part += f"; echoes at {', '.join(period_text(r.period_days) for r in echoes)}"
            parts.append(part + ")")
        text = f"Repeating pattern: {'; '.join(parts)}."
        if any(r.harmonic_of for r in peaks):
            text += (
                "\nEchoes are no rhythm of their own: a pattern of a few hard days also shows "
                "at a half and a third of its length, often stronger than the pattern itself."
            )
        return text

    def readout_at(self, x: float) -> str:
        """Period and amplitude of the point nearest to x = log10(period in days)."""
        if self.spectrum is None:
            return HOVER_HINT
        periods, amplitudes = self.spectrum.periods_days, self.spectrum.amplitudes
        i = min(range(len(periods)), key=lambda i: abs(math.log10(periods[i]) - x))
        self._cursor.setValue(math.log10(periods[i]))
        self._cursor.show()
        return f"Every {period_text(periods[i])}: ±{_amount(amplitudes[i])} {self._unit}/day"

    def _hover(self, scene_pos) -> None:  # noqa: ANN001
        vb = self.plot.getPlotItem().vb
        if self.spectrum is None or not vb.sceneBoundingRect().contains(scene_pos):
            return
        self.readout.setText(self.readout_at(vb.mapSceneToView(scene_pos).x()))
