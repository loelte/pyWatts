"""A progress chart as an image on white: title, legend, plot, summary and footer.

The chart is drawn again from a hidden copy in theme.PRINT_CHART colors at a fixed size, so
the image never depends on the dark theme or the size of the window."""

from __future__ import annotations

from datetime import date

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen

from pywatts.ui import theme
from pywatts.ui.theme import ChartColors
from pywatts.ui.widgets.power_curve import Chart, LegendEntry

WIDTH, HEIGHT = 1600, 1000
MARGIN = 64
SWATCH = 30  # legend mark width


def _font(px: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont()
    font.setPixelSize(px)
    font.setWeight(weight)
    return font


def render_chart(chart: Chart, rider: str = "", today: date | None = None) -> QImage:
    """The chart with its data, options and time span as shown, in light colors."""
    colors = theme.PRINT_CHART
    today = today or date.today()
    copy = chart.clone(colors)
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(colors.background))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        _draw(painter, copy, colors, rider, today)
    finally:
        painter.end()
        copy.deleteLater()
    return image


def _draw(painter: QPainter, chart: Chart, colors: ChartColors, rider: str, today: date) -> None:
    left, right = MARGIN, WIDTH - MARGIN
    inner = right - left
    y = MARGIN

    title = _font(34, QFont.Weight.DemiBold)
    painter.setFont(title)
    painter.setPen(QColor(colors.heading))
    height = QFontMetrics(title).height()
    painter.drawText(QRect(left, y, inner, height), Qt.AlignmentFlag.AlignLeft, chart.title())
    y += height + 4

    subtitle = _font(18)
    painter.setFont(subtitle)
    painter.setPen(QColor(colors.muted))
    height = QFontMetrics(subtitle).height()
    text = f"{rider}  ·  {chart.subtitle()}" if rider else chart.subtitle()
    painter.drawText(QRect(left, y, inner, height), Qt.AlignmentFlag.AlignLeft, text)
    y += height + 22

    entries = chart.legend_entries()
    if entries:
        y += _legend(painter, entries, colors, left, y) + 18

    footer = _font(15)
    footer_height = QFontMetrics(footer).height()
    footer_top = HEIGHT - MARGIN // 2 - footer_height
    painter.setPen(QPen(QColor(colors.axis), 1))
    painter.drawLine(QPointF(left, footer_top - 14), QPointF(right, footer_top - 14))
    painter.setFont(footer)
    painter.setPen(QColor(colors.muted))
    row = QRect(left, footer_top, inner, footer_height)
    painter.drawText(row, Qt.AlignmentFlag.AlignLeft, "pyWatts")
    painter.drawText(row, Qt.AlignmentFlag.AlignRight, f"Exported {today:%Y-%m-%d}")

    bottom = footer_top - 14 - 24
    summary = chart.export_summary
    if summary:
        body = _font(19)
        flags = Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap
        height = (
            QFontMetrics(body).boundingRect(QRect(0, 0, inner, HEIGHT), flags, summary).height()
        )
        bottom -= height
        painter.setFont(body)
        painter.setPen(QColor(colors.text))
        painter.drawText(QRect(left, bottom, inner, height), flags, summary)
        bottom -= 24

    plot = _plot(chart, colors, inner, bottom - y)
    painter.drawImage(QPointF(left, y), plot)


def _legend(
    painter: QPainter, entries: list[LegendEntry], colors: ChartColors, x: int, y: int
) -> int:
    """One row of marks and texts; returns its height."""
    font = _font(17)
    metrics = QFontMetrics(font)
    height = metrics.height()
    middle = y + height / 2
    painter.setFont(font)
    for entry in entries:
        color = QColor(entry.color)
        if entry.mark == "bar":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(x + 6, middle - 9, 18, 18), 3, 3)
        else:
            style = Qt.PenStyle.DashLine if entry.mark == "dash" else Qt.PenStyle.SolidLine
            width = colors.line_width - (1 if entry.mark == "dash" else 0)
            painter.setPen(QPen(color, width, style, Qt.PenCapStyle.FlatCap))
            painter.drawLine(QPointF(x, middle), QPointF(x + SWATCH, middle))
            if entry.mark == "dots":
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                radius = colors.symbol_size / 2
                painter.drawEllipse(QPointF(x + SWATCH / 2, middle), radius, radius)
        x += SWATCH + 10
        painter.setPen(QColor(colors.text))
        width = metrics.horizontalAdvance(entry.text)
        painter.drawText(QRect(x, y, width + 2, height), Qt.AlignmentFlag.AlignLeft, entry.text)
        x += width + 36
    return height


def _plot(chart: Chart, colors: ChartColors, width: int, height: int) -> QImage:
    """The plot laid out at this size. pyqtgraph's exporter draws in export mode: round
    markers instead of the cached ones, which a painter moved on the page would misplace."""
    from pyqtgraph.exporters import ImageExporter  # loads the parameter tree

    item = chart.plot.getPlotItem()
    item.setGeometry(QRectF(0, 0, width, height))
    layout = item.layout
    if layout is not None:
        layout.activate()
    exporter = ImageExporter(item)
    exporter.parameters()["background"] = QColor(colors.background)
    image = exporter.export(toBytes=True)
    if not isinstance(image, QImage):
        raise RuntimeError("the chart could not be drawn")
    return image
