"""Best power per duration of one ride: list and curve, compared with the all-time best."""

from __future__ import annotations

import math

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pywatts.storage.repo import Peak
from pywatts.ui import theme
from pywatts.ui.widgets.common import text_label
from pywatts.ui.widgets.power_curve import NO_VALUE, TICK_SECONDS, duration_label

COLUMNS = ["Duration", "W", "W/kg", "% best"]
_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter


class RidePeaksPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = text_label("muted", "Best power in this ride")
        self.plot = pg.PlotWidget()
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        for axis in ("left", "bottom"):
            self.plot.getAxis(axis).enableAutoSIPrefix(False)
        self.plot.setLabel("left", "W")
        self.plot.getAxis("bottom").setTicks(
            [[(math.log10(d), duration_label(d)) for d in TICK_SECONDS]]
        )
        self.plot.setMinimumHeight(140)
        self.plot.setMinimumWidth(260)
        self._all_time = self.plot.plot(
            pen=pg.mkPen(theme.MUTED, width=1, style=Qt.PenStyle.DashLine)
        )
        self._ride = self.plot.plot(
            pen=pg.mkPen(theme.POWER, width=2),
            symbol="o",
            symbolSize=6,
            symbolBrush=theme.POWER,
            symbolPen=None,
        )
        self.plot.addLegend()
        self.plot.getPlotItem().legend.addItem(self._all_time, "All-time best")
        self.plot.getPlotItem().legend.addItem(self._ride, "This ride")

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        vertical = self.table.verticalHeader()
        assert vertical is not None
        vertical.hide()
        vertical.setDefaultSectionSize(26)
        self.table.setShowGrid(False)
        for col in range(len(COLUMNS)):
            self.table.horizontalHeaderItem(col).setTextAlignment(_LEFT if col == 0 else _RIGHT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.title)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.table, 2)

    def set_peaks(
        self,
        ride: dict[int, float],
        best: dict[int, Peak],
        weight_kg: float | None,
    ) -> None:
        """ride: watts per duration (s) of the shown ride; best: all-time best per duration."""
        durations = sorted(ride)
        self.table.setRowCount(len(durations))
        for row, d in enumerate(durations):
            watts = ride[d]
            ref = best.get(d)
            cells = [
                duration_label(d),
                f"{watts:.0f}",
                f"{watts / weight_kg:.2f}" if weight_kg else NO_VALUE,
                f"{100 * watts / ref.watts:.0f}%" if ref and ref.watts > 0 else NO_VALUE,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(_LEFT if col == 0 else _RIGHT)
                self.table.setItem(row, col, item)
        self._ride.setData([math.log10(d) for d in durations], [ride[d] for d in durations])
        shared = sorted(best)
        self._all_time.setData([math.log10(d) for d in shared], [best[d].watts for d in shared])
        self.title.setText("Best power in this ride" if ride else "No power data in this ride")
        if durations:
            top = max([*ride.values(), *(best[d].watts for d in shared)])
            self.plot.setXRange(
                math.log10(min(durations[0], 3)) - 0.05,
                math.log10(max(durations[-1], 3600)) + 0.05,
                padding=0,
            )
            self.plot.setYRange(0, top * 1.1, padding=0)
