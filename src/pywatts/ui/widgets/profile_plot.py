"""Workout profile chart shared by editor and ride page."""

from __future__ import annotations

import math

import pyqtgraph as pg
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QPainterPath, QPolygonF
from PyQt6.QtWidgets import QGraphicsPathItem, QWidget

from pywatts.core.workout import Block
from pywatts.core.zones import ZONE_COUNT, Zones, power_zones, split_by_zone
from pywatts.ui import theme

SPRINT_HEIGHT = 1.5  # drawn height of open blocks, fraction of FTP


class ProfilePlot(pg.PlotWidget):
    """Target profile in watts over minutes, colored by zone; optional cursor and power trace."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseEnabled(x=False, y=False)
        self.setMenuEnabled(False)
        self.hideButtons()
        self.setLabel("left", "W")
        self.setLabel("bottom", "min")
        for axis in ("left", "bottom"):
            self.getAxis(axis).enableAutoSIPrefix(False)
        self.showGrid(y=True, alpha=0.12)
        self.pieces: list[tuple[Block, int]] = []  # what is drawn: block part, zone
        self._areas: dict[int, QGraphicsPathItem] = {}
        for zone, color in theme.ZONE_COLORS.items():
            area = QGraphicsPathItem()
            area.setBrush(pg.mkBrush(color))
            area.setPen(pg.mkPen(theme.SURFACE, width=1))  # gap between blocks
            self.addItem(area, ignoreBounds=True)
            self._areas[zone] = area
        sprint = QGraphicsPathItem()  # no target: a pale outline instead of a solid block
        color = pg.mkColor(theme.ZONE_COLORS[ZONE_COUNT])
        sprint.setPen(pg.mkPen(color, width=2, style=Qt.PenStyle.DashLine))
        color.setAlpha(70)
        sprint.setBrush(pg.mkBrush(color))
        self.addItem(sprint, ignoreBounds=True)
        self._sprints = sprint
        self._has_sprints = False
        self._top = self._shown_top = 0.0
        self._trace = self.plot([], [], pen=pg.mkPen(theme.POWER_TRACE, width=2), connect="finite")
        self._cursor = pg.InfiniteLine(0, pen=pg.mkPen(theme.TEXT, width=2))
        self._cursor.hide()
        self.addItem(self._cursor)

    def set_profile(self, blocks: list[Block], ftp_w: int, zones: Zones | None = None) -> None:
        zones = zones or power_zones(ftp_w)
        erg = [b for b in blocks if not b.open]
        self.pieces = [piece for b in erg for piece in split_by_zone(b, ftp_w, zones)]
        paths = {zone: QPainterPath() for zone in theme.ZONE_COLORS}
        for b, zone in self.pieces:
            x0, x1 = b.t0 / 60, b.t1 / 60
            corners = ((x0, 0), (x0, b.p_start * ftp_w), (x1, b.p_end * ftp_w), (x1, 0))
            paths[zone].addPolygon(QPolygonF([QPointF(x, y) for x, y in corners]))
            paths[zone].closeSubpath()
        for zone, path in paths.items():
            self._areas[zone].setPath(path)
        sprints = QPainterPath()
        for b in blocks:
            if b.open:
                sprints.addRect(b.t0 / 60, 0, (b.t1 - b.t0) / 60, SPRINT_HEIGHT * ftp_w)
        self._sprints.setPath(sprints)
        self._has_sprints = len(erg) < len(blocks)
        peak = max((max(b.p_start, b.p_end) * ftp_w for b in erg), default=0)
        if self._has_sprints:
            peak = max(peak, SPRINT_HEIGHT * ftp_w)
        self.setXRange(0, blocks[-1].t1 / 60 if blocks else 1, padding=0)
        self._top = self._shown_top = max(peak, 100.0) * 1.2
        self.setYRange(0, self._top, padding=0)

    def set_cursor(self, t_s: float | None) -> None:
        self._cursor.setVisible(t_s is not None)
        if t_s is not None:
            self._cursor.setValue(t_s / 60)

    def set_trace(self, t_s: list[float], power_w: list[float]) -> None:
        """NaN in power_w breaks the line."""
        self._trace.setData([t / 60 for t in t_s], power_w)
        if self._has_sprints:  # sprint power is not known in advance: keep it in view
            best = max((p for p in power_w if math.isfinite(p)), default=0.0)
            top = max(self._top, best * 1.05)
            if top != self._shown_top:
                self._shown_top = top
                self.setYRange(0, top, padding=0)
