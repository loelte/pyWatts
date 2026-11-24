"""History ride chart: selectable channels, each on the left or the right y axis."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import pyqtgraph as pg
from PyQt6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QVBoxLayout, QWidget

from pywatts.storage.repo import Sample
from pywatts.ui import theme

LEFT, RIGHT = "left", "right"


@dataclass(frozen=True)
class Channel:
    key: str
    name: str
    unit: str
    color: str
    side: str  # default axis
    value: Callable[[Sample], float | None]


CHANNELS = (
    Channel("target", "Target", "W", theme.ACCENT, LEFT, lambda s: s.target_w or None),
    Channel("power", "Power", "W", theme.POWER, LEFT, lambda s: s.power),
    Channel("hr", "HR", "bpm", theme.ZONE_COLORS[5], RIGHT, lambda s: s.hr),
    Channel("cadence", "Cadence", "rpm", "#2dd4bf", RIGHT, lambda s: s.cadence),
    Channel("speed", "Speed", "km/h", theme.OK, RIGHT, lambda s: s.speed),
    Channel("altitude", "Altitude", "m", "#a78bfa", RIGHT, lambda s: s.altitude),
)


class RideChart(QWidget):
    """Samples over minutes. A channel is listed (checkbox, axis choice, legend) only when the
    ride has data for it. The checked state and the axis choice stay when another ride is shown."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plot = pg.PlotWidget()
        self.plot.setMenuEnabled(False)
        self.plot.setLabel("bottom", "min")
        self.plot.showGrid(y=True, alpha=0.2)
        item = self.plot.getPlotItem()
        self._left = item.vb
        self._right = pg.ViewBox()
        item.showAxis(RIGHT)
        item.scene().addItem(self._right)
        item.getAxis(RIGHT).linkToView(self._right)
        self._right.setXLink(item)
        self._left.sigResized.connect(self._sync_views)
        for axis in ("left", "bottom", "right"):
            item.getAxis(axis).enableAutoSIPrefix(False)
        self._legend = self.plot.addLegend()

        self.curves: dict[str, pg.PlotDataItem] = {}
        self.checkboxes: dict[str, QCheckBox] = {}
        self.axis_boxes: dict[str, QComboBox] = {}
        self._cells: dict[str, QWidget] = {}
        self._has_data: dict[str, bool] = {}
        self._side: dict[str, str] = {}
        bar = QHBoxLayout()
        for ch in CHANNELS:
            curve = pg.PlotDataItem(
                [], [], pen=pg.mkPen(ch.color, width=2), connect="finite", antialias=True
            )
            self._left.addItem(curve)
            self.curves[ch.key] = curve
            self._side[ch.key] = ch.side
            self._has_data[ch.key] = False

            check = QCheckBox(f"{ch.name} ({ch.unit})")
            check.setChecked(True)
            check.setStyleSheet(f"QCheckBox {{ color: {ch.color}; }}")
            axis = QComboBox()
            axis.addItems(["Left", "Right"])
            axis.setCurrentIndex(ch.side == RIGHT)
            axis.setToolTip(f"Y axis for {ch.name.lower()}")
            check.toggled.connect(lambda _on: self._apply())
            axis.currentIndexChanged.connect(lambda _i, k=ch.key: self._axis_changed(k))
            cell = QWidget()
            row = QHBoxLayout(cell)
            row.setContentsMargins(0, 0, 8, 0)
            row.setSpacing(4)
            row.addWidget(check)
            row.addWidget(axis)
            cell.hide()
            bar.addWidget(cell)
            self.checkboxes[ch.key], self.axis_boxes[ch.key], self._cells[ch.key] = (
                check,
                axis,
                cell,
            )
        bar.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.plot, 1)
        self._sync_views()
        self._apply()

    # --- public ---
    def set_samples(self, samples: list[Sample], has_target: bool = True) -> None:
        minutes = [s.t / 60 for s in samples]
        for ch in CHANNELS:
            values = [ch.value(s) for s in samples] if has_target or ch.key != "target" else []
            self._has_data[ch.key] = any(v is not None for v in values)
            if self._has_data[ch.key]:
                ys = [math.nan if v is None else float(v) for v in values]
                self.curves[ch.key].setData(minutes, ys)
            else:
                self.curves[ch.key].setData([], [])
        self._apply()

    def available(self) -> list[str]:
        return [k for k, has in self._has_data.items() if has]

    def shown(self) -> list[str]:
        return [k for k in self.available() if self.checkboxes[k].isChecked()]

    def side(self, key: str) -> str:
        return self._side[key]

    def set_side(self, key: str, side: str) -> None:
        self.axis_boxes[key].setCurrentIndex(side == RIGHT)

    # --- internals ---
    def _sync_views(self) -> None:
        self._right.setGeometry(self._left.sceneBoundingRect())
        self._right.linkedViewChanged(self._left, self._right.XAxis)

    def _axis_changed(self, key: str) -> None:
        self._side[key] = RIGHT if self.axis_boxes[key].currentIndex() else LEFT
        self._apply()

    def _apply(self) -> None:
        shown = self.shown()
        for ch in CHANNELS:
            curve = self.curves[ch.key]
            view = self._right if self._side[ch.key] == RIGHT else self._left
            other = self._left if view is self._right else self._right
            if curve in other.addedItems:
                other.removeItem(curve)
            if curve not in view.addedItems:
                view.addItem(curve)
            curve.setVisible(ch.key in shown)
            self._cells[ch.key].setVisible(self._has_data[ch.key])

        self._legend.clear()
        for ch in CHANNELS:
            if ch.key in shown:
                self._legend.addItem(self.curves[ch.key], f"{ch.name} ({ch.unit})")
        for axis, side in (("left", LEFT), ("right", RIGHT)):
            units: list[str] = []
            for ch in CHANNELS:
                if ch.key in shown and self._side[ch.key] == side and ch.unit not in units:
                    units.append(ch.unit)
            self.plot.setLabel(axis, " / ".join(units))
        right_used = any(self._side[k] == RIGHT for k in shown)
        self.plot.getPlotItem().getAxis(RIGHT).setVisible(right_used)
        self._left.enableAutoRange()
        self._right.enableAutoRange()
        self._sync_views()
