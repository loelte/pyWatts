"""Painted calendars: week, month and year. Widgets per day would be too slow for a year."""

from __future__ import annotations

import html
from datetime import date, timedelta

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QHelpEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPolygonF,
    QResizeEvent,
)
from PyQt6.QtWidgets import QApplication, QSizePolicy, QToolTip, QWidget

from pywatts.core.workout import Workout, flatten
from pywatts.core.zones import ZONE_COUNT, Zones, power_zones, split_by_zone
from pywatts.ui import theme
from pywatts.ui.widgets.calendar_model import (
    LOOKS,
    MONTHS,
    WEEKDAYS,
    CalendarData,
    Entry,
    Look,
    day_text,
    hours,
    main_entry,
    month_end,
    number,
    text_on,
    visible_range,
    year_fill,
)
from pywatts.ui.widgets.profile_plot import SPRINT_HEIGHT

SMALL, NORMAL, BOLD = 11, 12, 13  # px
RADIUS = 4.0
_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_CENTER = Qt.AlignmentFlag.AlignCenter
_DAY = timedelta(days=1)


def _font(base: QFont, size: int, bold: bool = False) -> QFont:
    font = QFont(base)
    font.setPixelSize(size)
    if bold:
        font.setWeight(QFont.Weight.DemiBold)
    return font


def wrap(text: str, metrics: QFontMetricsF, width: float, lines: int) -> list[str]:
    """Break at spaces; the last line ends with … if the text is too long."""
    words, result = text.split(), []
    while words and len(result) < lines - 1:
        line = words.pop(0)
        while words and metrics.horizontalAdvance(f"{line} {words[0]}") <= width:
            line += " " + words.pop(0)
        result.append(line)
    if words:
        result.append(" ".join(words))
    return [metrics.elidedText(x, Qt.TextElideMode.ElideRight, width) for x in result]


def workout_shape(
    workout: Workout, ftp_w: int, zones: Zones
) -> list[tuple[QPainterPath, int, bool]]:
    """(area, zone, sprint) in a unit square, y down."""
    blocks = flatten(workout)
    if not blocks:
        return []
    end = blocks[-1].t1
    # a common minimum height: easy rides look low beside hard ones
    top = max([1.2, *(SPRINT_HEIGHT if b.open else max(b.p_start, b.p_end) for b in blocks)])
    paths: dict[tuple[int, bool], QPainterPath] = {}
    for block in blocks:
        if block.open:
            pieces = [(block, ZONE_COUNT)]
        else:
            pieces = split_by_zone(block, ftp_w, zones)
        for piece, zone in pieces:
            x0, x1 = piece.t0 / end, piece.t1 / end
            ends = (SPRINT_HEIGHT,) * 2 if block.open else (piece.p_start, piece.p_end)
            y0, y1 = (1 - p / top for p in ends)
            path = paths.setdefault((zone, block.open), QPainterPath())
            path.addPolygon(
                QPolygonF([QPointF(x0, 1), QPointF(x0, y0), QPointF(x1, y1), QPointF(x1, 1)])
            )
            path.closeSubpath()
    return [(path, zone, sprint) for (zone, sprint), path in paths.items()]


def paint_look(painter: QPainter, rect: QRectF, look: Look) -> None:
    painter.setPen(Qt.PenStyle.NoPen if look.outline is None else QPen(QColor(look.outline), 1.5))
    painter.setBrush(Qt.BrushStyle.NoBrush if look.fill is None else QColor(look.fill))
    if look.outline is not None:
        rect = rect.adjusted(0.75, 0.75, -0.75, -0.75)
    painter.drawRoundedRect(rect, RADIUS, RADIUS)


class CalendarView(QWidget):
    """Base: days and entries as rectangles, with mouse, keyboard, drag and tooltips."""

    entry_clicked = pyqtSignal(object)  # Entry
    entry_activated = pyqtSignal(object)  # Entry: double click
    day_activated = pyqtSignal(object)  # date: double click, add something there
    day_opened = pyqtSignal(object)  # date: show it in more detail
    entry_moved = pyqtSignal(object, object)  # Entry, date
    move_refused = pyqtSignal(object)  # Entry
    cursor_left = pyqtSignal(object)  # date outside of the period

    NAME = ""
    DRAGGABLE = True

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName(f"{self.NAME} calendar")
        today = date.today()
        self.data = CalendarData(today, today, today)
        self.anchor = today
        self.cursor_day = today
        self._days: list[tuple[QRectF, date]] = []
        self._entries: list[tuple[QRectF, Entry]] = []
        self._more: list[tuple[QRectF, date, int]] = []
        self._selected: tuple[str, str] | None = None
        self._ftp, self._zones = 200, power_zones(200)
        self._stale = True
        self._press: tuple[QPoint, Entry | None, date | None, bool] | None = None
        self._last: tuple[Entry | None, date | None] = (None, None)
        self._drag: Entry | None = None
        self._drag_pos = QPoint()

    # --- public ---
    def set_data(self, data: CalendarData, anchor: date, ftp_w: int, zones: Zones) -> None:
        """anchor: a day of the shown period."""
        self.data, self.anchor = data, anchor
        self._ftp, self._zones = ftp_w, zones
        if not data.first <= self.cursor_day <= data.last:
            self.cursor_day = anchor
        self._end_drag()
        self._content_changed()
        self._invalidate()
        self._describe()

    def set_selected(self, key: tuple[str, str] | None) -> None:
        if key != self._selected:
            self._selected = key
            self.update()

    def set_cursor_day(self, day: date) -> None:
        self.cursor_day = day
        self._describe()
        self.update()

    @property
    def day_rects(self) -> list[tuple[QRectF, date]]:
        self._ensure_layout()
        return self._days

    @property
    def entry_rects(self) -> list[tuple[QRectF, Entry]]:
        self._ensure_layout()
        return self._entries

    @property
    def more_rects(self) -> list[tuple[QRectF, date, int]]:
        """With the number of entries that are not shown."""
        self._ensure_layout()
        return self._more

    def rect_of(self, key: tuple[str, str]) -> QRectF | None:
        return next((r for r, e in self.entry_rects if e.key == key), None)

    def rect_of_day(self, day: date) -> QRectF | None:
        return next((r for r, d in self.day_rects if d == day), None)

    def entry_at(self, pos: QPointF) -> Entry | None:
        return next((e for r, e in self.entry_rects if r.contains(pos)), None)

    def day_at(self, pos: QPointF) -> date | None:
        return next((d for r, d in self.day_rects if r.contains(pos)), None)

    # --- for subclasses ---
    def _layout(self) -> None:
        raise NotImplementedError

    def _paint(self, painter: QPainter) -> None:
        raise NotImplementedError

    def _content_changed(self) -> None:
        pass

    def _click_day(self, day: date) -> None:
        pass

    # --- layout ---
    def _invalidate(self) -> None:
        self._stale = True
        self.update()

    def _ensure_layout(self) -> None:
        if self._stale:
            self._stale = False
            self._days, self._entries, self._more = [], [], []
            self._layout()

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        super().resizeEvent(event)
        self._invalidate()

    def _describe(self) -> None:
        lines = [day_text(self.cursor_day)]
        lines += (f"{e.title}: {e.label}" for e in self.data.entries(self.cursor_day))
        self.setAccessibleDescription("\n".join(lines))

    # --- painting ---
    def paintEvent(self, _event: QPaintEvent | None) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint(painter)
        self._paint_marks(painter)
        painter.end()

    def _text(
        self,
        painter: QPainter,
        rect: QRectF,
        text: str,
        color: str,
        size: int = NORMAL,
        bold: bool = False,
        align: Qt.AlignmentFlag = _LEFT,
    ) -> None:
        font = _font(self.font(), size, bold)
        painter.setFont(font)
        painter.setPen(QColor(color))
        text = QFontMetricsF(font).elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, align | Qt.TextFlag.TextSingleLine, text)

    def _paint_day_box(self, painter: QPainter, rect: QRectF, day: date, dim: bool) -> None:
        today = day == self.data.today
        painter.setBrush(QColor(theme.WINDOW if dim else theme.SURFACE))
        painter.setPen(QPen(QColor(theme.ACCENT if today else theme.BORDER), 1.5 if today else 1))
        painter.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), 6, 6)

    def _paint_marks(self, painter: QPainter) -> None:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for rect, entry in self.entry_rects:
            if entry.key == self._selected:
                painter.setPen(QPen(QColor("#ffffff"), 2))
                painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), RADIUS + 1, RADIUS + 1)
        if self.hasFocus() and self._drag is None:
            rect = self.rect_of_day(self.cursor_day)
            if rect is not None:
                painter.setPen(QPen(QColor(theme.MUTED), 1, Qt.PenStyle.DashLine))
                painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        if self._drag is not None:
            self._paint_drag(painter, self._drag)

    def _paint_drag(self, painter: QPainter, entry: Entry) -> None:
        pos = QPointF(self._drag_pos)
        day = self.day_at(pos)
        rect = None if day is None or day == entry.day else self.rect_of_day(day)
        if rect is not None:
            painter.setPen(QPen(QColor(theme.ACCENT), 2))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)
        chip = QRectF(pos.x() + 10, pos.y() + 6, 120, 20)
        chip.moveRight(min(chip.right(), self.width() - 1.0))
        painter.setOpacity(0.9)
        self._paint_chip(painter, chip, entry)
        painter.setOpacity(1.0)

    def _paint_chip(self, painter: QPainter, rect: QRectF, entry: Entry) -> None:
        paint_look(painter, rect, entry.look)
        text = f"{entry.look.symbol} {entry.title}".strip()
        self._text(painter, rect.adjusted(5, 0, -3, 0), text, entry.look.text, SMALL)

    # --- mouse ---
    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        assert event is not None
        if event.button() is not Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        entry, day = self.entry_at(pos), self.day_at(pos)
        more = any(r.contains(pos) for r, _, _ in self.more_rects)
        self._press = (pos.toPoint(), entry, day, more)
        self._last = (entry, day)
        if day is not None:
            self.set_cursor_day(day)

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        assert event is not None
        pos = event.position().toPoint()
        if self._drag is not None:
            self._drag_pos = pos
            self.update()
            return
        if self._press is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return
        start, entry, _, _ = self._press
        if not self.DRAGGABLE or entry is None or entry.unit is None:
            return
        if (pos - start).manhattanLength() < QApplication.startDragDistance():
            return
        if entry.linked:
            self._press = None
            self.move_refused.emit(entry)
            return
        self._drag, self._drag_pos = entry, pos
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        assert event is not None
        if event.button() is not Qt.MouseButton.LeftButton:
            return
        press, drag = self._press, self._drag
        self._end_drag()
        if drag is not None:
            day = self.day_at(event.position())
            if day is not None and day != drag.day:
                self.entry_moved.emit(drag, day)
        elif press is not None:
            _, entry, day, more = press
            if more and day is not None:
                self.day_opened.emit(day)
            elif entry is not None:
                self.entry_clicked.emit(entry)
            elif day is not None:
                self._click_day(day)

    def mouseDoubleClickEvent(self, event: QMouseEvent | None) -> None:
        """Acts on what was pressed first: that click may have moved the layout."""
        assert event is not None
        if event.button() is not Qt.MouseButton.LeftButton:
            return
        self._end_drag()
        entry, day = self._last
        if entry is not None:
            self.entry_activated.emit(entry)
        elif day is not None:
            self.day_activated.emit(day)

    def _end_drag(self) -> None:
        self._press = self._drag = None
        self.unsetCursor()
        self.update()

    # --- keyboard ---
    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        assert event is not None
        key = event.key()
        K = Qt.Key
        steps: dict[int, int] = {K.Key_Left: -1, K.Key_Right: 1, K.Key_Up: -7, K.Key_Down: 7}
        if key == K.Key_Escape and self._drag is not None:
            self._end_drag()
        elif key in steps:
            self._step(timedelta(days=steps[key]))
        elif key in (K.Key_Return, K.Key_Enter, K.Key_Space):
            self._next_entry()
        elif key == K.Key_Insert:
            self.day_activated.emit(self.cursor_day)
        else:
            super().keyPressEvent(event)

    def _step(self, step: timedelta) -> None:
        day = self.cursor_day + step
        if self.data.first <= day <= self.data.last:
            self.set_cursor_day(day)
        else:
            self.cursor_left.emit(day)

    def _next_entry(self) -> None:
        """Select the first entry of the day, then the next one."""
        entries = self.data.entries(self.cursor_day)
        if entries:
            keys = [e.key for e in entries]
            index = keys.index(self._selected) + 1 if self._selected in keys else 0
            self.entry_clicked.emit(entries[index % len(entries)])

    # --- tooltips ---
    def tooltip_at(self, pos: QPointF) -> str:
        entry = self.entry_at(pos)
        if entry is not None:
            return entry.describe()
        hidden = next((day for r, day, _ in self.more_rects if r.contains(pos)), None)
        return "" if hidden is None else self._day_tooltip(hidden)

    def _day_tooltip(self, day: date) -> str:
        parts = [day_text(day), *(e.describe() for e in self.data.entries(day))]
        return "\n\n".join(parts)

    def event(self, event: QEvent | None) -> bool:
        if isinstance(event, QHelpEvent) and event.type() == QEvent.Type.ToolTip:
            text = self.tooltip_at(QPointF(event.pos()))
            # Titles may come from files: never as rich text.
            rich = f"<div style='white-space: pre'>{html.escape(text)}</div>" if text else ""
            QToolTip.showText(event.globalPos(), rich, self)
            return True
        return super().event(event)


class WeekView(CalendarView):
    NAME = "Week"
    HEAD, GAP, PAD, LINE = 26, 6, 6, 15
    SHAPE, TITLE_LINES = 34, 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shapes: dict[tuple[str, str], list[tuple[QPainterPath, int, bool]]] = {}

    def _title(self, entry: Entry, card_width: float) -> list[str]:
        metrics = QFontMetricsF(_font(self.font(), NORMAL, bold=True))
        return wrap(entry.title, metrics, card_width - 10, self.TITLE_LINES)

    @staticmethod
    def _values(entry: Entry) -> list[str]:
        values = []
        if entry.rideable and entry.duration_s is not None:
            values.append(hours(entry.duration_s))
        if entry.rideable and entry.load is not None:
            values.append(f"Load {number(entry.load)}")
        return values

    def _card_height(self, entry: Entry, title_lines: int) -> int:
        lines = 1 + title_lines + len(self._values(entry))
        return lines * self.LINE + 2 * self.PAD + (self.SHAPE + 6 if self._shape(entry) else 0)

    def _shape(self, entry: Entry) -> list[tuple[QPainterPath, int, bool]]:
        if entry.unit is None or entry.unit.workout is None:
            return []
        if entry.key not in self._shapes:
            self._shapes[entry.key] = workout_shape(entry.unit.workout, self._ftp, self._zones)
        return self._shapes[entry.key]

    def _content_changed(self) -> None:
        self._shapes.clear()
        heights = [
            sum(self._card_height(e, self.TITLE_LINES) + self.GAP for e in entries)
            for entries in self.data.days.values()
        ]
        # room to add: an empty strip below the last card
        self.setMinimumHeight(self.HEAD + max([60, *heights]) + 24)
        self.updateGeometry()

    def _layout(self) -> None:
        width = (self.width() - 1) / 7
        for i in range(7):
            day = self.data.first + timedelta(days=i)
            column = QRectF(i * width + 2, 1, width - 4, self.height() - 2)
            self._days.append((column, day))
            y = float(self.HEAD)
            for entry in self.data.entries(day):
                card = QRectF(column.x() + 4, y, column.width() - 8, 0)
                height = self._card_height(entry, len(self._title(entry, card.width())))
                card.setHeight(height)
                self._entries.append((card, entry))
                y += height + self.GAP

    def _paint(self, painter: QPainter) -> None:
        for rect, day in self.day_rects:
            self._paint_day_box(painter, rect, day, dim=False)
            today = day == self.data.today
            self._text(
                painter,
                QRectF(rect.x() + 2, rect.y(), rect.width() - 4, self.HEAD),
                f"{WEEKDAYS[day.weekday()]} {day.day}",
                theme.TEXT if today else theme.MUTED,
                bold=today,
                align=_CENTER,
            )
        for rect, entry in self.entry_rects:
            self._paint_card(painter, rect, entry)

    def _paint_card(self, painter: QPainter, card: QRectF, entry: Entry) -> None:
        paint_look(painter, card, entry.look)
        color = entry.look.text
        row = QRectF(card.x() + 6, card.y() + self.PAD, card.width() - 10, self.LINE)
        self._text(painter, row, entry.label, color, SMALL)
        for line in self._title(entry, card.width()):
            row.translate(0, self.LINE)
            self._text(painter, row, line, color, NORMAL, bold=True)
        row.translate(0, self.LINE)
        shape = self._shape(entry)
        if shape:
            box = QRectF(card.x() + 5, row.y() + 2, card.width() - 10, self.SHAPE)
            self._paint_shape(painter, box, shape)
            row.translate(0, self.SHAPE + 6)
        for text in self._values(entry):
            self._text(painter, row, text, color, SMALL)
            row.translate(0, self.LINE)

    def _paint_shape(
        self, painter: QPainter, box: QRectF, shape: list[tuple[QPainterPath, int, bool]]
    ) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.INPUT))
        painter.drawRoundedRect(box, 3, 3)
        painter.save()
        painter.translate(box.x() + 2, box.y() + 2)
        painter.scale(box.width() - 4, box.height() - 4)
        for path, zone, sprint in shape:
            color = QColor(theme.ZONE_COLORS[zone])
            if sprint:
                color.setAlpha(120)
            painter.fillPath(path, color)
        painter.restore()


class MonthView(CalendarView):
    NAME = "Month"
    HEAD, SUMS, CHIP, NUMBER = 20, 88, 18, 18
    MIN_ROW = 46  # the number and one entry

    def _content_changed(self) -> None:
        self.updateGeometry()

    def _weeks(self) -> int:
        return ((self.data.last - self.data.first).days + 1) // 7

    def minimumSizeHint(self) -> QSize:
        return QSize(7 * 60 + self.SUMS, self.HEAD + self._weeks() * self.MIN_ROW)

    def _layout(self) -> None:
        width = (self.width() - self.SUMS) / 7
        height = (self.height() - self.HEAD) / max(1, self._weeks())
        for i in range(self._weeks() * 7):
            day = self.data.first + timedelta(days=i)
            cell = QRectF(i % 7 * width + 1, self.HEAD + i // 7 * height + 1, width - 2, height - 2)
            self._days.append((cell, day))
            self._layout_day(cell, day)

    def _layout_day(self, cell: QRectF, day: date) -> None:
        entries = self.data.entries(day)
        step = self.CHIP + 2
        top = cell.y() + self.NUMBER + 2
        fits = max(0, int((cell.bottom() - 2 - top) // step))
        # A full cell keeps its last line for "+n more"; a tiny one puts it beside the number.
        shown = entries if len(entries) <= fits else entries[: max(1, fits - 1)]
        if fits == 0:
            shown = []
        for i, entry in enumerate(shown):
            chip = QRectF(cell.x() + 3, top + i * step, cell.width() - 6, self.CHIP)
            self._entries.append((chip, entry))
        hidden = len(entries) - len(shown)
        if hidden:
            y = top + len(shown) * step
            if y + self.CHIP > cell.bottom():
                more = QRectF(cell.x() + 26, cell.y() + 2, cell.width() - 29, self.NUMBER)
            else:
                more = QRectF(cell.x() + 3, y, cell.width() - 6, self.CHIP)
            self._more.append((more, day, hidden))

    def _paint(self, painter: QPainter) -> None:
        width = (self.width() - self.SUMS) / 7
        for i, name in enumerate(WEEKDAYS):
            rect = QRectF(i * width, 0, width, self.HEAD)
            self._text(painter, rect, name, theme.MUTED, SMALL, align=_CENTER)
        week = QRectF(7 * width + 8, 0, self.SUMS - 8, self.HEAD)
        self._text(painter, week, "Week", theme.MUTED, SMALL)
        for i, (rect, day) in enumerate(self.day_rects):
            dim = day.month != self.anchor.month
            self._paint_day_box(painter, rect, day, dim)
            today = day == self.data.today
            color = theme.TEXT if today else theme.DISABLED if dim else theme.MUTED
            at = QRectF(rect.x() + 6, rect.y() + 2, rect.width() - 10, self.NUMBER)
            self._text(painter, at, str(day.day), color, SMALL, bold=today)
            if i % 7 == 0:
                box = QRectF(week.x(), rect.y(), week.width(), rect.height())
                self._paint_sums(painter, box, day)
        for rect, entry in self.entry_rects:
            self._paint_chip(painter, rect, entry)
        for rect, _, hidden in self.more_rects:
            text = f"+{hidden}" if rect.width() < 50 else f"+{hidden} more"
            self._text(painter, rect.adjusted(3, 0, -2, 0), text, theme.MUTED, SMALL)

    def _paint_sums(self, painter: QPainter, rect: QRectF, monday: date) -> None:
        sums = self.data.totals(monday, monday + 6 * _DAY)
        line = QRectF(rect.x(), rect.y() + 2, rect.width(), 15)
        if sums.planned:
            self._text(painter, line, f"Load {sums.planned_load:.0f}", theme.MUTED, SMALL)
            line.translate(0, 15)
            self._text(painter, line, hours(sums.planned_s), theme.MUTED, SMALL)
            line.translate(0, 15)
        if sums.done:
            self._text(painter, line, f"✓ Load {sums.done_load:.0f}", theme.OK, SMALL)
            line.translate(0, 15)
            if line.bottom() <= rect.bottom():
                self._text(painter, line, f"✓ {hours(sums.done_s)}", theme.OK, SMALL)

    def tooltip_at(self, pos: QPointF) -> str:
        if pos.x() > self.width() - self.SUMS:
            row = next((d for r, d in self.day_rects[::7] if r.y() <= pos.y() <= r.bottom()), None)
            if row is not None:
                return self.data.totals(row, row + 6 * _DAY).text().replace("   ·   ", "\n")
        return super().tooltip_at(pos)


class YearView(CalendarView):
    NAME = "Year"
    DRAGGABLE = False
    HEAD, GAP = 20, 14

    def minimumSizeHint(self) -> QSize:
        return QSize(420, 4 * (self.HEAD + 6 * 12))

    def _columns(self) -> int:
        """Of months; the split with the most even day squares."""

        def shape(columns: int) -> float:
            width = (self.width() / columns - self.GAP) / 7
            height = (self.height() / (12 // columns) - self.HEAD - self.GAP / 2) / 6
            return min(width, height * 1.6)

        return max((2, 3, 4, 6), key=shape)

    def _month_rects(self) -> list[QRectF]:
        columns = self._columns()
        width = (self.width() + self.GAP) / columns
        height = self.height() / (12 // columns)
        return [
            QRectF(i % columns * width, i // columns * height, width - self.GAP, height - 8)
            for i in range(12)
        ]

    def _layout(self) -> None:
        for month, box in enumerate(self._month_rects(), start=1):
            start = date(self.data.first.year, month, 1)
            monday = visible_range("month", start)[0]
            width, height = box.width() / 7, (box.height() - self.HEAD) / 6
            for n in range(month_end(start).day):
                day = start + timedelta(days=n)
                place = (day - monday).days
                cell = QRectF(
                    box.x() + place % 7 * width,
                    box.y() + self.HEAD + place // 7 * height,
                    width - 2,
                    height - 2,
                )
                self._days.append((cell, day))

    def _paint(self, painter: QPainter) -> None:
        for month, box in enumerate(self._month_rects()):
            head = QRectF(box.x() + 2, box.y(), box.width(), self.HEAD)
            this = (self.data.today.year, self.data.today.month) == (
                self.data.first.year,
                month + 1,
            )
            color = theme.TEXT if this else theme.MUTED
            self._text(painter, head, MONTHS[month], color, NORMAL, bold=True)
        for rect, day in self.day_rects:
            self._paint_day(painter, rect, day)

    def _paint_day(self, painter: QPainter, rect: QRectF, day: date) -> None:
        entries = self.data.entries(day)
        entry = main_entry(entries)
        fill = year_fill(entries)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(fill or theme.SURFACE))
        painter.drawRoundedRect(rect, 3, 3)
        outline = entry.look.outline if entry is not None else None
        if outline is not None:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(outline), 1.5))
            painter.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), 3, 3)
        symbol = "" if entry is None else entry.look.symbol
        color = theme.DISABLED if entry is None else theme.TEXT if fill is None else text_on(fill)
        if day == self.data.today:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#ffffff"), 1.5))
            painter.drawRoundedRect(rect.adjusted(-0.5, -0.5, 0.5, 0.5), 3, 3)
            if entry is None:
                color = theme.TEXT
        size = 10 if rect.height() >= 13 else 8
        if symbol and rect.width() >= 25:
            self._text(painter, rect.adjusted(3, 0, 0, 0), str(day.day), color, size)
            self._text(painter, rect.adjusted(0, 0, -3, 0), symbol, color, size, align=_RIGHT)
        else:
            self._text(painter, rect, symbol or str(day.day), color, size, align=_CENTER)

    def _click_day(self, day: date) -> None:
        self.day_opened.emit(day)

    def mouseDoubleClickEvent(self, event: QMouseEvent | None) -> None:
        pass  # the first click already left this view

    def _next_entry(self) -> None:
        self.day_opened.emit(self.cursor_day)

    def tooltip_at(self, pos: QPointF) -> str:
        day = self.day_at(pos)
        return "" if day is None or not self.data.entries(day) else self._day_tooltip(day)


class Legend(QWidget):
    """Meaning of the colors, on one line; what does not fit is left out."""

    STATES = ("indoor", "outdoor", "done", "partial", "missed", "skipped", "ride")
    _LABELS = {"skipped": "Skipped, rest, note"}
    BOX, SPACE = 16, 14

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(18)
        self.setAccessibleName("Colors")

    def paintEvent(self, _event: QPaintEvent | None) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = _font(self.font(), SMALL)
        metrics = QFontMetricsF(font)
        painter.setFont(font)
        x = 1.0
        for state in self.STATES:
            look = LOOKS[state]
            label = self._LABELS.get(state, look.label)
            width = metrics.horizontalAdvance(label)
            if x + self.BOX + 6 + width > self.width():
                break
            box = QRectF(x, 1, self.BOX, self.BOX)
            paint_look(painter, box, look)
            painter.setPen(QColor(look.text))
            painter.drawText(box, _CENTER, look.symbol)
            painter.setPen(QColor(theme.MUTED))
            x += self.BOX + 6
            painter.drawText(QRectF(x, 0, width + 2, 18), _LEFT, label)
            x += width + self.SPACE
        painter.end()
