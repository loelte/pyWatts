"""Detail panel of the training plan: one unit or one unplanned ride."""

from __future__ import annotations

from PyQt6.QtCore import QElapsedTimer, QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QShowEvent
from PyQt6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pywatts.core.plan import Ride, compliance
from pywatts.core.workout import flatten
from pywatts.core.zones import Zones
from pywatts.storage.repo import SessionRow
from pywatts.ui import theme
from pywatts.ui.ride import NO_VALUE
from pywatts.ui.widgets.calendar_model import Entry, day_text, hours, number, one_line, sprints
from pywatts.ui.widgets.common import text_label
from pywatts.ui.widgets.profile_plot import ProfilePlot

HOW = {"started": "started from the plan", "auto": "matched", "manual": "linked by hand"}
BELOW_HEIGHT, RIDE_HEIGHT, SIDE_WIDTH = 250, 140, 340
_NO_LIMIT = 16_777_215  # QWIDGETSIZE_MAX
_TONES = {"done": "ok", "missed": "warn", "partial": "warn"}


def _ride_name(session: SessionRow | None) -> str:
    if session is None:
        return "Ride"
    at = session.started_at_utc.astimezone()
    return f"{one_line(session.workout.name) or 'Ride'}, {at:%Y-%m-%d %H:%M}"


def ride_text(ride: Ride, session: SessionRow | None) -> list[str]:
    lines = [f"Ridden: {hours(ride.duration_s)}, load {number(ride.load)}"]
    if session is not None:
        values = (
            ("Avg", session.avg_power, "W"),
            ("NP", session.np, "W"),
            ("HR", session.avg_hr, "bpm"),
        )
        parts = [f"{name} {value:.0f} {unit}" for name, value, unit in values if value is not None]
        if session.kj is not None:
            parts.append(f"{session.kj:.0f} kJ")
        if parts:
            lines.append(", ".join(parts))
    return lines


def unit_text(entry: Entry, session: SessionRow | None, how: str | None) -> list[str]:
    unit = entry.unit
    assert unit is not None
    lines = []
    if unit.rideable:
        estimate = " without sprints" if sprints(unit) else ""
        lines.append(f"Planned: {hours(entry.planned_s)}, load {number(entry.planned_load)}")
        lines[0] += estimate
    if entry.ride is not None:
        ridden = ride_text(entry.ride, session)
        part = compliance(unit, entry.ride)
        if part is not None:
            ridden[0] += f" ({part:.0%})"
        lines += ridden
        lines.append(f"Ride: {_ride_name(session)} ({HOW.get(how or '', 'linked')})")
    elif entry.linked:
        lines.append("The ride is not finished yet.")
    return lines


class DetailPanel(QFrame):
    """Shows; the page acts on the buttons."""

    closed = pyqtSignal()
    # key, label, role
    ACTIONS = (
        ("ride", "Ride", "primary"),
        ("library", "Library", ""),
        ("edit", "Edit", ""),
        ("move", "Move", ""),
        ("skip", "Unskip", ""),
        ("link", "Link ride", ""),
        ("delete", "Delete", "danger"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        theme.set_role(self, "card")
        self.entry: Entry | None = None
        self._opened = QElapsedTimer()
        self.title = text_label("heading")
        self.title.setMinimumWidth(40)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status = text_label("chip")
        self.subtitle = text_label("muted")
        self.subtitle.setMinimumWidth(40)
        self.subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.close_button = QToolButton()
        self.close_button.setText("✕")
        self.close_button.setAutoRaise(True)
        self.close_button.setToolTip("Close")
        self.close_button.setAccessibleName("Close details")
        self.close_button.clicked.connect(lambda: self.closed.emit())
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(self.title, 1)
        head.addWidget(self.status)
        head.addWidget(self.close_button)

        self.plot = ProfilePlot()
        self.plot.setMinimumSize(120, 90)
        self.facts = text_label("")
        self.facts.setWordWrap(True)
        self.facts.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.facts.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setTabChangesFocus(True)
        self.notes.setMinimumHeight(36)
        self.notes.setAccessibleName("Notes")
        text = QVBoxLayout()
        text.setSpacing(6)
        text.addWidget(self.subtitle)
        text.addWidget(self.facts)
        text.addWidget(self.notes, 1)
        text.addStretch(0)
        self._body = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._body.setSpacing(10)
        self._body.addWidget(self.plot, 3)
        self._body.addLayout(text, 4)

        self.buttons: dict[str, QPushButton] = {}
        self._grid = QGridLayout()
        self._grid.setSpacing(6)
        for key, label, role in self.ACTIONS:
            button = QPushButton(label)
            button.setAutoDefault(False)
            if role:
                theme.set_role(button, role)
            button.installEventFilter(self)
            self.buttons[key] = button
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 10, 10)
        layout.setSpacing(8)
        layout.addLayout(head)
        layout.addLayout(self._body, 1)
        layout.addLayout(self._grid)
        self._side: bool | None = None
        self.set_side(False)

    def showEvent(self, event: QShowEvent | None) -> None:
        super().showEvent(event)
        self._opened.start()

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        """The second click of a double click must not hit a button that just appeared."""
        return (
            event is not None
            and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
            and self._opened.isValid()
            and self._opened.elapsed() < QApplication.doubleClickInterval()
        ) or super().eventFilter(obj, event)

    def set_side(self, side: bool) -> None:
        """Beside the calendar: tall and narrow; else a strip below it."""
        if side == self._side:
            return
        self._side = side
        direction = QBoxLayout.Direction.TopToBottom if side else QBoxLayout.Direction.LeftToRight
        self._body.setDirection(direction)
        self._resize()
        self.plot.setMaximumHeight(200 if side else _NO_LIMIT)
        columns = 3 if side else len(self.ACTIONS)
        for i, button in enumerate(self.buttons.values()):
            self._grid.addWidget(button, i // columns, i % columns)

    def _resize(self) -> None:
        is_unit = self.entry is None or self.entry.unit is not None
        height = BELOW_HEIGHT if is_unit else RIDE_HEIGHT
        side = bool(self._side)
        self.setMinimumSize(*((SIDE_WIDTH, 0) if side else (0, height)))
        self.setMaximumSize(*((SIDE_WIDTH, _NO_LIMIT) if side else (_NO_LIMIT, height)))

    def show_unit(
        self,
        entry: Entry,
        plan_name: str | None,
        session: SessionRow | None,
        how: str | None,
        in_library: bool,
        ftp_w: int,
        zones: Zones,
    ) -> None:
        unit = entry.unit
        assert unit is not None
        origin = "AI coach" if unit.origin == "ai" else None
        self._show(entry, unit_text(entry, session, how), unit.notes, plan_name, origin)
        blocks = flatten(unit.workout) if unit.workout is not None else []
        self.plot.setVisible(bool(blocks))
        if blocks:
            self.plot.set_profile(blocks, ftp_w, zones)
        for button in self.buttons.values():
            button.show()
        enabled = {
            "ride": bool(blocks),
            "library": in_library,
            "move": not entry.linked,
            "skip": not entry.linked and unit.rideable,
            "link": unit.rideable,
        }
        for key, on in enabled.items():
            self.buttons[key].setEnabled(on)
        self.buttons["skip"].setText("Unskip" if unit.skipped else "Skip")
        self.buttons["link"].setText("Unlink" if entry.linked else "Link ride")
        self.buttons["ride"].setToolTip("" if blocks else "Only indoor units with a workout.")
        tip = "Linked to a ride: unlink it first." if entry.linked else ""
        self.buttons["move"].setToolTip(tip)

    def show_ride(self, entry: Entry, session: SessionRow | None) -> None:
        assert entry.ride is not None
        lines = ride_text(entry.ride, session)
        if session is not None:
            started = session.started_at_utc.astimezone()
            lines.append(f"Started {started:%H:%M}, FTP {session.ftp_w} W, {session.status}")
        self._show(entry, lines, "")
        self.plot.hide()
        for button in self.buttons.values():
            button.hide()

    def _show(self, entry: Entry, lines: list[str], notes: str, *origin: str | None) -> None:
        self.entry = entry
        self._resize()
        self.title.setText(entry.title or NO_VALUE)
        self.status.setText("○ Planned" if entry.state in ("indoor", "outdoor") else entry.label)
        theme.set_role(self.status, _TONES.get(entry.state, ""), "tone")
        kind = "Ride" if entry.unit is None else entry.unit.kind.capitalize()
        about = [day_text(entry.day), kind, *(one_line(text) for text in origin if text)]
        self.subtitle.setText("  ·  ".join(about))
        self.facts.setText("\n".join(lines))
        self.facts.setVisible(bool(lines))
        if self.notes.toPlainText() != notes:  # keeps the scroll position otherwise
            self.notes.setPlainText(notes)
        self.notes.setVisible(bool(notes.strip()))
