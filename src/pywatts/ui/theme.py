"""Dark theme and zone colors. Variants are dynamic properties, see `set_role`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyqtgraph as pg
from PyQt6.QtCore import QByteArray, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication, QWidget

NAV = "#0d0f13"
WINDOW = "#12151a"
SURFACE = "#1a1e25"  # cards
INPUT = "#12151a"  # fields on a card
RAISED = "#252b34"
HOVER = "#2f3641"
BORDER = "#2b313b"
BORDER_STRONG = "#3b4350"
TEXT = "#e9ecf1"
MUTED = "#a3abb8"
DISABLED = "#69717f"
ACCENT = "#3b82f6"
PRIMARY = "#2563eb"  # white text on ACCENT would be too faint
PRIMARY_HOVER = "#3574f0"
SELECTED = "#1d3354"
OK = "#4cc38a"
WARN = "#f5b454"
ERROR = "#ff7b72"
POWER = "#ffb000"

# Z1 grey, Z2 blue, Z3 green, Z4 orange, Z5 red
ZONE_COLORS = {1: "#8b929c", 2: "#3b82f6", 3: "#2fa65a", 4: "#f08c1a", 5: "#e5484d"}
# Units of the training plan. Text on them: calendar_model picks what is readable.
PLAN_COLORS = {
    "indoor": "#7cc4ff",
    "outdoor": "#1e4fa3",
    "done": OK,
    "missed": ZONE_COLORS[5],
    "neutral": "#4a5260",  # rest, note, skipped, unplanned ride
}
ON_ZONE = "#0b0c0e"  # text on a zone color
POWER_TRACE = "#ffffff"


@dataclass(frozen=True)
class ChartColors:
    """Colors and sizes of the progress charts: on screen, or exported on white."""

    background: str
    heading: str
    text: str  # tick labels, axis titles, summary
    muted: str
    axis: str  # axis line and ticks; the grid is this color with grid_alpha
    grid_alpha: float
    first: str  # all time, fitness, bars, trends
    second: str  # recent, fatigue, averages
    third: str  # form
    reference: str  # FTP line
    zero: str  # zero line, hover cursor
    line_width: float
    symbol_size: int
    font_px: int | None  # None: the application font
    line_labels: bool  # names on reference lines; the export has a legend instead


SCREEN_CHART = ChartColors(
    background=SURFACE,
    heading=TEXT,
    text=MUTED,
    muted=MUTED,
    axis=MUTED,
    grid_alpha=0.12,
    first=ACCENT,
    second=POWER,
    third=OK,
    reference=MUTED,
    zero=BORDER_STRONG,
    line_width=2,
    symbol_size=6,
    font_px=None,
    line_labels=True,
)
# The screen colors are made for a dark background: amber and green fade on white.
PRINT_CHART = ChartColors(
    background="#ffffff",
    heading="#111827",
    text="#374151",
    muted="#6b7280",
    axis="#9ca3af",
    grid_alpha=0.3,
    first="#2563eb",
    second="#d97706",
    third="#059669",
    reference="#6b7280",
    zero="#9ca3af",
    line_width=3,
    symbol_size=9,
    font_px=16,
    line_labels=False,
)

_ASSETS = (Path(__file__).parent / "assets").as_posix()

_STYLE = f"""
QWidget {{ color: {TEXT}; }}
QLabel:disabled, QCheckBox:disabled {{ color: {DISABLED}; }}
QToolTip {{
    background: {RAISED}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; padding: 4px 6px;
}}

/* text */
QLabel[role="title"] {{ font-size: 22px; font-weight: 600; }}
QLabel[role="heading"] {{ font-size: 14px; font-weight: 600; }}
QLabel[role="muted"] {{ color: {MUTED}; }}
QLabel[role="chip"] {{
    background: {RAISED}; color: {MUTED}; border-radius: 10px; padding: 3px 10px;
}}
QLabel[role="chip"][tone="ok"] {{ color: {OK}; background: #16302a; }}
QLabel[role="chip"][tone="warn"] {{ color: {WARN}; background: #352a18; }}
QLabel[role="error"] {{
    color: {ERROR}; background: #321d20; border-radius: 6px; padding: 8px 12px;
}}

/* navigation */
QFrame#nav {{ background: {NAV}; border: none; border-right: 1px solid {BORDER}; }}
QLabel#brand {{ font-size: 18px; font-weight: 700; padding: 4px 8px 14px 8px; }}
QToolButton#rider {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 7px 18px 7px 10px; margin-bottom: 10px; text-align: left;
}}
QToolButton#rider:hover {{ border-color: {BORDER_STRONG}; }}
QToolButton#rider::menu-indicator {{
    subcontrol-origin: padding; subcontrol-position: center right; right: 6px;
}}
QListWidget#sidebar {{ background: transparent; border: none; padding: 0; }}
QListWidget#sidebar::item {{
    color: {MUTED}; border-radius: 8px; padding: 9px 10px; margin: 1px 0;
}}
QListWidget#sidebar::item:hover {{ background: {SURFACE}; color: {TEXT}; }}
QListWidget#sidebar::item:selected {{ background: {SELECTED}; color: #ffffff; }}

/* cards */
QFrame[role="card"], QGroupBox {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px;
}}
QGroupBox {{ font-weight: 600; padding: 34px 6px 4px 6px; }}
QGroupBox::title {{
    /* offsets, not padding: Qt leaves the border out behind the title rectangle */
    subcontrol-origin: border; subcontrol-position: top left; left: 17px; top: 12px;
}}

/* lists and tables */
QListWidget, QTreeWidget, QTableWidget {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px;
    outline: 0; padding: 4px; selection-background-color: {SELECTED};
}}
QListWidget:focus, QTreeWidget:focus, QTableWidget:focus {{ border-color: {BORDER_STRONG}; }}
QListWidget::item {{ border-radius: 6px; padding: 8px; }}
QTreeWidget::item {{ padding: 5px 4px; border: none; }}
QTableWidget::item {{ padding: 0 5px; border: none; border-bottom: 1px solid {BORDER}; }}
QListWidget::item:hover, QTreeWidget::item:hover {{ background: {RAISED}; }}
QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected {{
    background: {SELECTED}; color: #ffffff;
}}
QTreeView::branch, QTreeView::branch:selected {{ background: transparent; }}
QGroupBox QTableWidget {{
    background: transparent; border: none; border-radius: 0; padding: 0;
}}
QGroupBox QListWidget {{ background: {INPUT}; border-radius: 6px; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
    background: transparent; color: {MUTED}; border: none; font-weight: 400;
    border-bottom: 1px solid {BORDER_STRONG}; padding: 6px 8px;
}}

/* inputs */
QLineEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox {{
    background: {INPUT}; border: 1px solid {BORDER_STRONG}; border-radius: 6px;
    selection-background-color: {PRIMARY};
}}
QLineEdit, QAbstractSpinBox, QComboBox {{ padding: 4px 8px; min-height: 20px; }}
QAbstractSpinBox QLineEdit {{ border: none; background: transparent; padding: 0; min-height: 0; }}
QPlainTextEdit {{ padding: 2px 4px; }}
QLineEdit:hover, QPlainTextEdit:hover, QAbstractSpinBox:hover, QComboBox:hover {{
    border-color: {DISABLED};
}}
QLineEdit:focus, QPlainTextEdit:focus, QAbstractSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled {{
    color: {DISABLED}; background: transparent; border-color: {BORDER};
}}
QAbstractSpinBox {{ padding: 2px 22px 2px 8px; min-height: 24px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    subcontrol-origin: border; width: 20px; border: none; background: transparent;
}}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; }}
QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; }}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
    background: {RAISED}; border-radius: 5px;
}}
QAbstractSpinBox::up-arrow {{ image: url("{_ASSETS}/up.svg"); width: 10px; height: 10px; }}
QAbstractSpinBox::down-arrow, QDateEdit::down-arrow, QComboBox::down-arrow {{
    image: url("{_ASSETS}/down.svg"); width: 10px; height: 10px;
}}
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::down-arrow:disabled,
QComboBox::down-arrow:disabled {{ image: none; }}
QDateEdit::drop-down, QComboBox::drop-down {{
    subcontrol-origin: border; subcontrol-position: center right; width: 24px; border: none;
}}
QComboBox QAbstractItemView {{
    background: {RAISED}; border: 1px solid {BORDER_STRONG}; outline: 0;
    selection-background-color: {SELECTED};
}}
QCheckBox::indicator {{
    width: 16px; height: 16px; border: 1px solid {BORDER_STRONG}; border-radius: 4px;
    background: {INPUT};
}}
QCheckBox::indicator:hover, QCheckBox::indicator:focus {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background: {PRIMARY}; border-color: {PRIMARY}; image: url("{_ASSETS}/check.svg");
}}

/* buttons */
QPushButton {{
    background: {RAISED}; border: 1px solid {BORDER_STRONG}; border-radius: 6px;
    padding: 5px 12px; min-height: 18px;
}}
QPushButton:hover {{ background: {HOVER}; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton[scale="large"] {{ min-height: 34px; font-size: 15px; }}
QPushButton[role="primary"] {{
    background: {PRIMARY}; border-color: {PRIMARY}; color: #ffffff; font-weight: 600;
}}
QPushButton[role="primary"]:hover {{ background: {PRIMARY_HOVER}; }}
QPushButton[role="primary"]:focus {{ border-color: #ffffff; }}
QPushButton[role="primary"]:pressed {{ background: #1d4fc4; }}
QPushButton[role="danger"] {{ color: {ERROR}; background: transparent; }}
QPushButton[role="danger"]:hover {{ background: #321d20; border-color: {ERROR}; }}
QPushButton:disabled, QPushButton[role]:disabled {{
    color: {DISABLED}; background: transparent; border-color: {BORDER};
}}

/* scrolling and splitting */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {BORDER_STRONG}; border-radius: 3px; }}
QScrollBar::handle:vertical {{ min-height: 28px; }}
QScrollBar::handle:horizontal {{ min-width: 28px; }}
QScrollBar::handle:hover {{ background: {DISABLED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {BORDER_STRONG}; }}

/* training plan */
QPushButton[role="segment"] {{ border-radius: 0; margin-left: -1px; padding: 5px 10px; }}
QPushButton[role="segment"][end="left"] {{
    border-top-left-radius: 6px; border-bottom-left-radius: 6px; margin-left: 0;
}}
QPushButton[role="segment"][end="right"] {{
    border-top-right-radius: 6px; border-bottom-right-radius: 6px;
}}
QPushButton[role="segment"]:checked {{ background: {SELECTED}; color: #ffffff; }}
QPushButton[role="icon"] {{ padding: 5px 6px; min-width: 16px; }}
QPushButton::menu-indicator {{ image: url("{_ASSETS}/down.svg"); width: 10px; height: 10px;
    subcontrol-origin: padding; subcontrol-position: center right; right: 8px;
}}
QPushButton[role="menu"] {{ padding-right: 24px; }}
QMenu {{ background: {RAISED}; border: 1px solid {BORDER_STRONG}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {SELECTED}; }}
QMenu::item:disabled {{ color: {DISABLED}; }}
"""

# 24x24 line icons of the sidebar
_ICONS = {
    "library": "M4 6h16M4 12h16M4 18h10",
    "ride": "M13 3 5 14h6l-1 7 8-11h-6z",
    "history": "M12 4a8 8 0 1 1 0 16a8 8 0 1 1 0-16M12 8v4l3 2",
    "training plan": "M5 6h14v14H5zM5 10h14M9 3v5M15 3v5M9 15l2 2 4-4",
    "progress": "M4 19h16M5 15l4-5 4 3 6-8",
    "profile": "M12 4a4 4 0 1 1 0 8a4 4 0 1 1 0-8M5 20c0-7 14-7 14 0",
    "sync": "M4 11a8 8 0 0 1 14-4l2 2M20 4v5h-5M20 13a8 8 0 0 1-14 4l-2-2M4 20v-5h5",
    "settings": "M4 7h8M18 7h2M12 7a3 3 0 1 1 6 0a3 3 0 1 1-6 0"
    "M4 17h2M12 17h8M6 17a3 3 0 1 1 6 0a3 3 0 1 1-6 0",
}


def apply(app: QApplication) -> None:
    """Call before any widget is created."""
    app.setStyle("Fusion")
    hints = app.styleHints()
    if hints is not None and hasattr(hints, "setColorScheme"):  # Qt 6.8+: dark title bar
        hints.setColorScheme(Qt.ColorScheme.Dark)
    font = app.font()
    if 0 < font.pointSizeF() < 10:
        font.setPointSizeF(10)
        app.setFont(font)
    palette = QPalette()
    role = QPalette.ColorRole
    for r, color in (
        (role.Window, WINDOW),
        (role.Base, SURFACE),
        (role.AlternateBase, RAISED),
        (role.Button, RAISED),
        (role.ToolTipBase, RAISED),
        (role.WindowText, TEXT),
        (role.Text, TEXT),
        (role.ButtonText, TEXT),
        (role.ToolTipText, TEXT),
        (role.PlaceholderText, MUTED),
        (role.Highlight, PRIMARY),
        (role.HighlightedText, "#ffffff"),
        (role.Link, ACCENT),
    ):
        palette.setColor(r, QColor(color))
    for r in (role.Text, role.WindowText, role.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, r, QColor(DISABLED))
    app.setPalette(palette)
    app.setStyleSheet(_STYLE)
    pg.setConfigOptions(background=SURFACE, foreground=MUTED, antialias=True)


def set_role(widget: QWidget, role: str, name: str = "role") -> None:
    """Set a dynamic property the style sheet selects on; cheap if unchanged."""
    if widget.property(name) == role:
        return
    widget.setProperty(name, role)
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)


def zone_icon(zone: int) -> QIcon:
    pixmap = QPixmap(36, 36)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ZONE_COLORS[zone]))
    painter.drawRoundedRect(2, 2, 32, 32, 8, 8)
    painter.end()
    return QIcon(pixmap)


def nav_icon(name: str) -> QIcon:
    icon = QIcon()
    for mode, color in ((QIcon.Mode.Normal, MUTED), (QIcon.Mode.Selected, "#ffffff")):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            f'stroke="{color}" stroke-width="1.8" stroke-linecap="round" '
            f'stroke-linejoin="round"><path d="{_ICONS[name]}"/></svg>'
        )
        pixmap = QPixmap(72, 72)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        QSvgRenderer(QByteArray(svg.encode())).render(painter)
        painter.end()
        icon.addPixmap(pixmap, mode)
    return icon
