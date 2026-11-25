"""Small shared building blocks of the pages."""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPaintEvent
from PyQt6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QLabel,
    QLayout,
    QLayoutItem,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pywatts.ui import theme

PAGE_MARGIN = 20
GAP = 12


def page_layout(page: QWidget, layout: QLayout) -> None:
    layout.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
    layout.setSpacing(GAP)


def pad_spin_boxes(page: QWidget) -> None:
    """Style sheet padding does not reach the text of number boxes."""
    for kind in (QSpinBox, QDoubleSpinBox):
        for box in page.findChildren(kind):
            edit = box.lineEdit()
            if edit is not None:
                edit.setTextMargins(6, 0, 0, 0)


def text_label(role: str, text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    theme.set_role(label, role)
    return label


def card(*widgets: QWidget, margin: int = 10) -> QFrame:
    """Rounded panel, e.g. around a plot."""
    frame = QFrame()
    theme.set_role(frame, "card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(margin, margin, margin, margin)
    for widget in widgets:
        layout.addWidget(widget)
    return frame


class FlowLayout(QLayout):
    """Left to right, wrapping into more rows. It is only as wide as its widest widget, so its
    content never widens the panel it is in."""

    def __init__(self, spacing: int = 4) -> None:
        super().__init__()
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem | None) -> None:
        if item is not None:
            self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, move=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect: QRect, move: bool) -> int:
        """The height that is needed. Hidden widgets take no room."""
        x, y, row = rect.x(), rect.y(), 0
        for item in self._items:
            if item.isEmpty():
                continue
            size = item.sizeHint()
            if row and x + size.width() > rect.x() + rect.width():
                x, y, row = rect.x(), y + row + self.spacing(), 0
            if move:
                item.setGeometry(QRect(QPoint(x, y), size))
            x += size.width() + self.spacing()
            row = max(row, size.height())
        return y + row - rect.y()


class ValueLabel(QLabel):
    """Draws "250 W" with a large number and a small unit. text() stays the plain string."""

    def __init__(self, text: str, size: int) -> None:
        super().__init__(text)
        self._size = size
        self._color: str | None = None
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_size(self, size: int) -> None:
        if size != self._size:
            self._size = size
            self.updateGeometry()
            self.update()

    def set_color(self, color: str | None) -> None:
        """None: default colors."""
        self._color = color
        self.update()

    def _fonts(self) -> tuple[QFont, QFont]:
        number, unit = QFont(self.font()), QFont(self.font())
        number.setPixelSize(self._size)
        number.setWeight(QFont.Weight.DemiBold)
        unit.setPixelSize(max(13, round(self._size * 0.34)))
        return number, unit

    def sizeHint(self) -> QSize:
        number, unit = self._fonts()
        # room for the longest usual value, so that tiles do not resize during a ride
        width = QFontMetricsF(number).horizontalAdvance("0000")
        width += QFontMetricsF(unit).horizontalAdvance(" km/h")
        return QSize(round(width), round(QFontMetricsF(number).height()))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, _event: QPaintEvent | None) -> None:
        value, _, unit = self.text().partition(" ")
        number_font, unit_font = self._fonts()
        metrics = QFontMetricsF(number_font)
        width = metrics.horizontalAdvance(value)
        gap = self._size * 0.12 if unit else 0.0
        total = width + gap + QFontMetricsF(unit_font).horizontalAdvance(unit)
        x = max(0.0, (self.width() - total) / 2)
        baseline = (self.height() - metrics.height()) / 2 + metrics.ascent()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setPen(QColor(self._color or theme.TEXT))
        painter.setFont(number_font)
        painter.drawText(QRectF(x, baseline - metrics.ascent(), width + 1, metrics.height()), value)
        painter.setPen(QColor(self._color or theme.MUTED))
        painter.setFont(unit_font)
        painter.drawText(round(x + width + gap), round(baseline), unit)
        painter.end()
