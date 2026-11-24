"""Terminal emulator on the screen model of pyte, and the widget that shows it.

The output is untrusted: every sequence is bounded, and none can reach the host (clipboard, title,
window). pyte's own parser is not used, as it misreads private sequences and works per character.
"""

from __future__ import annotations

import copy
import logging
import re
import sys
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from functools import lru_cache
from typing import Any, NamedTuple

from PyQt6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QContextMenuEvent,
    QFocusEvent,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QInputMethodEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QWheelEvent,
)
from PyQt6.QtWidgets import QScrollBar, QWidget
from pyte import charsets
from pyte import modes as mo
from pyte.screens import Margins, Screen, StaticDefaultDict
from wcwidth import wcwidth

from pywatts.ui import theme

log = logging.getLogger(__name__)

SCROLLBACK = 5000
SLICE_CHARS = 512
BUDGET_S = 0.008
YIELD_MS = 2
FRAME_MS = 20
SYNC_S = 0.15  # longest wait for the end of a synchronized update
RESIZE_MS = 80
PADDING = 6
MIN_GRID = (2, 10)
MAX_GRID = (300, 500)
SEQUENCE_CHARS = 256
STRING_CHARS = 64 * 1024  # an unterminated OSC, DCS etc. ends here
MARKS = 8  # combining characters per cell

FOREGROUND, BACKGROUND = theme.TEXT, theme.WINDOW
PALETTE = (
    "#4a5260 #ff7b72 #4cc38a #f5b454 #5c9dff #c792ea #4fc1d0 #c5cad3 "
    "#69717f #ff9b94 #76dba8 #ffce80 #8bbcff #dcb3f5 #83dbe6 #ffffff"
).split()
_LEVELS = (0x00, 0x5F, 0x87, 0xAF, 0xD7, 0xFF)
PALETTE += [f"#{r:02x}{g:02x}{b:02x}" for r in _LEVELS for g in _LEVELS for b in _LEVELS]
PALETTE += [f"#{v:02x}{v:02x}{v:02x}" for v in range(8, 248, 10)]

_width = lru_cache(maxsize=8192)(wcwidth)


class Cell(NamedTuple):
    """As pyte.screens.Char, with other colors: "" default, int palette, "#rrggbb"."""

    data: str = " "
    fg: int | str = ""
    bg: int | str = ""
    bold: bool = False
    dim: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False


Style = tuple  # a Cell without data
PLAIN: Style = tuple(Cell()[1:])


class Run(NamedTuple):
    """Neighboring cells of one style. Characters that are not ASCII are alone in their run."""

    x: int
    text: str
    style: Style
    cells: int


Line = tuple[Run, ...]
Row = StaticDefaultDict


def line_runs(row: Row, columns: int) -> Line:
    """Without plain empty cells."""
    runs: list[Run] = []
    plain = row.default == (" ", *PLAIN)
    if not row:  # usual, and must be fast when empty lines are scrolled
        return () if plain else (Run(0, " " * columns, tuple(row.default[1:]), columns),)
    start, text, style, end = 0, "", PLAIN, -1
    for x in sorted(row) if plain else range(columns):
        if x >= columns:
            break
        cell = row[x]
        data, cell_style = cell[0], tuple(cell[1:])
        if not data:  # second half of a wide character
            continue
        simple = len(data) == 1 and " " <= data < "\x7f"
        if simple and x == end and cell_style == style:
            text += data
            end += 1
            continue
        if text:
            runs.append(Run(start, text, style, end - start))
            text, end = "", -1
        if data == " " and cell_style == PLAIN:
            continue
        if simple:
            start, text, style, end = x, data, cell_style, x + 1
        else:
            wide = x + 1 < columns and not row[x + 1][0]
            runs.append(Run(x, data, cell_style, 2 if wide else 1))
    if text:
        runs.append(Run(start, text, style, end - start))
    return tuple(runs)


def line_text(line: Line, first: int = 0, last: int = MAX_GRID[1]) -> str:
    """Of the columns [first, last), without trailing spaces."""
    columns: list[str] = []
    for run in line:
        columns += [" "] * (run.x - len(columns))
        if run.text.isascii() and len(run.text) == run.cells:
            columns += run.text
        else:
            columns += [run.text] + [""] * (run.cells - 1)
    return "".join(columns[first:last]).rstrip()


class TermScreen(Screen):
    """Corrects pyte where it deviates from terminals, raises or grows without limit."""

    def __init__(self, columns: int, lines: int, reply: Callable[[str], object]) -> None:
        self.history: deque[Line] = deque(maxlen=SCROLLBACK)
        self.pushed = 0  # lines that ever went to the history
        self.keep_history = True
        self.write_process_input = reply  # type: ignore[method-assign,assignment]
        super().__init__(columns, lines)

    @property
    def default_char(self) -> Any:
        return Cell()

    def reset(self) -> None:
        super().reset()
        self.savepoints = []
        self.cursor.attrs = self.default_char

    def blank(self) -> Any:
        return Cell(bg=self.cursor.attrs.bg)

    def _blank_row(self) -> Row:
        return StaticDefaultDict(self.blank())

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        lines, columns = lines or self.lines, columns or self.columns
        lost = max(0, self.cursor.y - lines + 1)
        if lost:
            self._push(lost)
            self._move(0, self.lines - 1, lost)
            self.cursor.y -= lost
        for y in [y for y in self.buffer if y >= lines]:
            del self.buffer[y]
        for row in self.buffer.values():
            for x in [x for x in row if x >= columns]:
                del row[x]
        self.lines, self.columns = lines, columns
        self.margins = None
        self.tabstops = set(range(8, columns, 8))
        self.ensure_hbounds()
        self.ensure_vbounds()
        self.dirty.update(range(lines))

    # --- text ---

    def draw(self, data: str) -> None:
        if (table := self.g1_charset if self.charset else self.g0_charset) is charsets.VT100_MAP:
            data = data.translate(table)
        cursor, columns = self.cursor, self.columns
        wrap, insert = mo.DECAWM in self.mode, mo.IRM in self.mode
        style = cursor.attrs[1:]
        row = self.buffer[cursor.y]
        self.dirty.add(cursor.y)
        for char in data:
            width = 1 if char < "\x7f" else _width(char)
            if width <= 0:
                self._combine(row, char)
                continue
            if cursor.x + width > columns:
                if wrap:
                    cursor.x = 0
                    self.index()
                    row = self.buffer[cursor.y]
                    self.dirty.add(cursor.y)
                else:
                    cursor.x = columns - width
            if insert:
                self.insert_characters(width)
            row[cursor.x] = Cell(char, *style)
            if width == 2:
                row[cursor.x + 1] = Cell("", *style)
            cursor.x += width

    def _combine(self, row: Row, char: str) -> None:
        x = self.cursor.x - 1
        if x > 0 and not row[x][0]:
            x -= 1
        if x >= 0 and (row[x][0].strip()) and len(row[x][0]) <= MARKS:
            text = unicodedata.normalize("NFC", row[x][0] + char)
            row[x] = row[x]._replace(data=text)

    # --- scrolling ---

    def _region(self) -> tuple[int, int]:
        top, bottom = self.margins or Margins(0, self.lines - 1)
        return top, bottom

    def _push(self, count: int) -> None:
        if self.keep_history:
            for y in range(count):
                self.history.append(line_runs(self.buffer[y], self.columns))
            self.pushed += count

    def _move(self, top: int, bottom: int, count: int) -> None:
        """Scroll lines up (count > 0) or down, and clear those that become free."""
        rows = [self.buffer[y] for y in range(top, bottom + 1)]
        size = len(rows)
        up, down = min(max(count, 0), size), min(max(-count, 0), size)
        kept = rows[up : size - down]
        rows = [self._blank_row() for _ in range(down)] + kept
        rows += [self._blank_row() for _ in range(up)]
        for y, row in enumerate(rows, top):
            self.buffer[y] = row
        self.dirty.update(range(top, bottom + 1))

    def scroll_up(self, count: int = 1) -> None:
        top, bottom = self._region()
        count = min(count or 1, bottom - top + 1)
        if top == 0:
            self._push(count)
        self._move(top, bottom, count)

    def scroll_down(self, count: int = 1) -> None:
        top, bottom = self._region()
        self._move(top, bottom, -(count or 1))

    def index(self) -> None:
        _, bottom = self._region()
        if self.cursor.y == bottom:
            self.scroll_up()
        elif self.cursor.y < self.lines - 1:
            self.cursor.y += 1

    def reverse_index(self) -> None:
        top, _ = self._region()
        if self.cursor.y == top:
            self.scroll_down()
        elif self.cursor.y > 0:
            self.cursor.y -= 1

    def insert_lines(self, count: int | None = None) -> None:
        self._shift_lines(-(count or 1))

    def delete_lines(self, count: int | None = None) -> None:
        self._shift_lines(count or 1)

    def _shift_lines(self, count: int) -> None:
        top, bottom = self._region()
        if top <= self.cursor.y <= bottom:
            self._move(self.cursor.y, bottom, count)
            self.cursor.x = 0

    # --- editing a line ---

    def insert_characters(self, count: int | None = None) -> None:
        self._shift_cells(count or 1, 0)

    def delete_characters(self, count: int | None = None) -> None:
        self._shift_cells(0, count or 1)

    def _shift_cells(self, insert: int, delete: int) -> None:
        x = min(self.cursor.x, self.columns - 1)
        row = self.buffer[self.cursor.y]
        cells = [row[i] for i in range(x + delete, self.columns)]
        blank = self.blank()
        cells = [blank] * min(insert, self.columns - x) + cells
        cells += [blank] * (self.columns - x - len(cells))
        for i, cell in enumerate(cells[: self.columns - x], x):
            row[i] = cell
        self.dirty.add(self.cursor.y)

    def _erase(self, first: int, last: int) -> None:
        row, blank = self.buffer[self.cursor.y], self.blank()
        for x in range(first, last):
            row[x] = blank
        self.dirty.add(self.cursor.y)

    def erase_characters(self, count: int | None = None) -> None:
        x = min(self.cursor.x, self.columns - 1)
        self._erase(x, min(x + (count or 1), self.columns))

    def erase_in_line(self, how: int = 0, private: bool = False) -> None:
        x = min(self.cursor.x, self.columns - 1)
        if how == 2:
            self.buffer[self.cursor.y] = self._blank_row()
            self.dirty.add(self.cursor.y)
        elif how == 1:
            self._erase(0, x + 1)
        elif how == 0:
            self._erase(x, self.columns)

    def erase_in_display(self, how: int = 0, *args: Any, **kwargs: Any) -> None:
        if how == 3:
            self.history.clear()
            return
        if how not in (0, 1, 2):
            return
        y = self.cursor.y
        rows = {0: range(y + 1, self.lines), 1: range(y), 2: range(self.lines)}[how]
        for row in rows:
            self.buffer[row] = self._blank_row()
        self.dirty.update(rows)
        if how != 2:
            self.erase_in_line(how)

    # --- cursor ---

    def cursor_up(self, count: int | None = None) -> None:
        top, _ = self._region()
        limit = top if self.cursor.y >= top else 0
        self.cursor.y = max(self.cursor.y - (count or 1), limit)

    def cursor_down(self, count: int | None = None) -> None:
        _, bottom = self._region()
        limit = bottom if self.cursor.y <= bottom else self.lines - 1
        self.cursor.y = min(self.cursor.y + (count or 1), limit)

    def cursor_to_line(self, line: int | None = None) -> None:
        self.cursor_position(line, min(self.cursor.x, self.columns - 1) + 1)

    def back_tab(self, count: int | None = None) -> None:
        for _ in range(min(count or 1, self.columns)):
            self.cursor.x = max((t for t in self.tabstops if t < self.cursor.x), default=0)

    def save_cursor(self) -> None:
        super().save_cursor()
        del self.savepoints[:-1]

    def restore_cursor(self) -> None:
        saved = self.savepoints[-1:]
        if saved:
            self.savepoints = [saved[0]._replace(cursor=copy.copy(saved[0].cursor))]
        super().restore_cursor()
        self.savepoints = saved

    def position(self) -> tuple[int, int]:
        """Line and column as reported to the program, from 1."""
        line = self.cursor.y + 1
        if self.margins is not None and mo.DECOM in self.mode:
            line -= self.margins.top
        return line, min(self.cursor.x, self.columns - 1) + 1


_TOKEN = re.compile(
    r"(?P<text>[^\x00-\x1f\x7f-\x9f]+)"
    r"|(?P<control>[\x00-\x1a\x1c-\x1f\x7f-\x9f])"
    r"|\x1b\[(?P<params>[0-?]*)(?P<inter>[ -/]*)(?P<final>[@-~])"
    r"|\x1b(?P<string>[\]PX^_])"
    r"|\x1b(?P<esc>[ -/]*[0-Z\\`-~])"
)
_UNFINISHED = re.compile(r"\x1b(?:\[[0-?]*[ -/]*|[ -/]*)")
_STRING_END = re.compile(r"[\x07\x18\x1a\x1b\x9c]")

_CSI = {
    "@": "insert_characters",
    "A": "cursor_up",
    "B": "cursor_down",
    "C": "cursor_forward",
    "D": "cursor_back",
    "E": "cursor_down1",
    "F": "cursor_up1",
    "G": "cursor_to_column",
    "H": "cursor_position",
    "I": "tab",
    "J": "erase_in_display",
    "K": "erase_in_line",
    "L": "insert_lines",
    "M": "delete_lines",
    "P": "delete_characters",
    "S": "scroll_up",
    "T": "scroll_down",
    "X": "erase_characters",
    "Z": "back_tab",
    "`": "cursor_to_column",
    "a": "cursor_forward",
    "d": "cursor_to_line",
    "e": "cursor_down",
    "f": "cursor_position",
    "g": "clear_tab_stop",
    "s": "save_cursor",
    "u": "restore_cursor",
}
_ESC = {
    "7": "save_cursor",
    "8": "restore_cursor",
    "D": "index",
    "H": "set_tab_stop",
    "M": "reverse_index",
}
_FLAGS = {1: "bold", 2: "dim", 3: "italics", 4: "underscore", 7: "reverse", 9: "strikethrough"}
_OFF = {
    21: {"underscore": True},  # double underline
    22: {"bold": False, "dim": False},
    23: {"italics": False},
    24: {"underscore": False},
    27: {"reverse": False},
    29: {"strikethrough": False},
    39: {"fg": ""},
    49: {"bg": ""},
}


class Emulator:
    """Feeds program output into the screen. No Qt."""

    def __init__(self, rows: int, cols: int, reply: Callable[[str], object]) -> None:
        self._reply = reply
        self.screen = TermScreen(cols, rows, reply)
        self._reset_modes()
        self._pending = ""  # start of a sequence
        self._string = ""  # kind of the string sequence being read
        self._body = ""
        self._string_size = 0

    def _reset_modes(self) -> None:
        self.title = ""
        self.app_cursor = False
        self.bracketed_paste = False
        self.focus_events = False
        self.synchronized = False
        self._main: tuple | None = None  # the main screen while the alternate one is shown
        self._last = ""

    @property
    def alternate(self) -> bool:
        return self._main is not None

    @property
    def cursor_visible(self) -> bool:
        return mo.DECTCEM in self.screen.mode

    @property
    def history_size(self) -> int:
        return 0 if self.alternate else len(self.screen.history)

    def line(self, number: int) -> Line:
        """By absolute number: the first line of the screen is `screen.pushed`."""
        screen = self.screen
        row = number - screen.pushed
        if 0 <= row < screen.lines:
            return line_runs(screen.buffer[row], screen.columns)
        if -self.history_size <= row < 0:
            return screen.history[row + len(screen.history)]
        return ()

    def resize(self, rows: int, cols: int) -> None:
        screen = self.screen
        if self._main is not None:
            # both in turn
            alternate = (screen.buffer, screen.cursor)
            screen.buffer, screen.cursor = self._main[:2]
            old = screen.lines, screen.columns
            screen.keep_history = True
            screen.resize(rows, cols)
            screen.lines, screen.columns = old
            self._main = (screen.buffer, screen.cursor, self._main[2], None)
            screen.buffer, screen.cursor = alternate
            screen.keep_history = False
        screen.resize(rows, cols)

    def feed(self, data: str) -> None:
        """Never raises."""
        data, self._pending = self._pending + data, ""
        position, end = 0, len(data)
        while position < end:
            if self._string:
                position = self._read_string(data, position)
                continue
            match = _TOKEN.match(data, position)
            if match is None:
                unfinished = _UNFINISHED.match(data, position)
                assert unfinished is not None  # only ESC matches no token
                if unfinished.end() == end and end - position <= SEQUENCE_CHARS:
                    self._pending = data[position:]
                    return
                position = unfinished.end()  # corrupt
                continue
            position = match.end()
            try:
                self._token(match)
            except Exception:
                log.debug("bad terminal sequence", exc_info=True)
                self.screen.ensure_hbounds()
                self.screen.ensure_vbounds()

    def _token(self, match: re.Match[str]) -> None:
        kind = match.lastgroup
        if kind == "text":
            text = match["text"]
            self.screen.draw(text)
            self._last = text[-1]
        elif kind == "control":
            self._control(match["control"])
        elif kind == "final":
            self._csi(match["params"][:SEQUENCE_CHARS], match["inter"], match["final"])
        elif kind == "string":
            self._string, self._body, self._string_size = match["string"], "", 0
        else:
            self._escape(match["esc"])

    def _control(self, char: str) -> None:
        screen = self.screen
        if char in "\n\v\f":
            screen.linefeed()
        elif char == "\r":
            screen.cursor.x = 0
        elif char == "\b":
            screen.backspace()
        elif char == "\t":
            screen.tab()
        elif char == "\x0e":
            screen.shift_out()
        elif char == "\x0f":
            screen.shift_in()

    def _escape(self, code: str) -> None:
        screen = self.screen
        if code == "c":
            if self._main is not None:
                screen.buffer = self._main[0]
            screen.keep_history = True
            screen.reset()
            self._reset_modes()
        elif code == "E":
            screen.cursor.x = 0
            screen.index()
        elif code in _ESC:
            getattr(screen, _ESC[code])()
        elif len(code) == 2 and code[0] in "()":
            screen.define_charset(code[1], mode=code[0])

    # --- OSC, DCS, SOS, PM, APC ---

    def _read_string(self, data: str, position: int) -> int:
        match = _STRING_END.search(data, position)
        stop = len(data) if match is None else match.start()
        self._string_size += stop - position
        if len(self._body) < SEQUENCE_CHARS:
            self._body += data[position : min(stop, position + SEQUENCE_CHARS)]
        if self._string_size > STRING_CHARS:
            self._string = ""
            return stop
        if match is None:
            return stop
        char = match.group()
        if char == "\x1b":
            if stop + 1 == len(data):
                self._pending = char
                return stop + 1
            if data[stop + 1] != "\\":  # aborted by the next sequence
                self._string = ""
                return stop
            stop += 1
        elif char == "\x07" and self._string != "]":
            return stop + 1
        elif char in "\x18\x1a":
            self._string = ""
            return stop + 1
        if self._string == "]" and self._string_size <= SEQUENCE_CHARS:
            self._osc(self._body)
        self._string = ""
        return stop + 1

    def _osc(self, body: str) -> None:
        code, _, value = body.partition(";")
        if code in ("0", "2"):
            self.title = value
        elif code in ("10", "11") and value == "?":
            color = FOREGROUND if code == "10" else BACKGROUND
            parts = "/".join(color[i : i + 2] * 2 for i in (1, 3, 5))
            self._reply(f"\x1b]{code};rgb:{parts}\x1b\\")

    # --- CSI ---

    def _csi(self, params: str, inter: str, final: str) -> None:
        screen = self.screen
        prefix = params[:1] if params[:1] in ("<", "=", ">", "?") else ""
        body = params[len(prefix) :]
        if final == "m":
            if not prefix and not inter:
                self._style(body)
            return
        if not re.fullmatch(r"[0-9;]*", body):
            return
        numbers = [min(int(p[-9:] or 0), 9999) for p in body.split(";")]
        first = numbers[0]
        if inter:
            if inter == "$" and final == "p" and prefix == "?":
                self._reply(f"\x1b[?{first};{self._mode_state(first)}$y")
            elif inter == "!" and final == "p":
                self._soft_reset()
        elif final in "hl":
            if prefix in ("", "?"):
                for number in numbers:
                    self._set_mode(number, final == "h", bool(prefix))
        elif prefix == ">" and final == "c":
            self._reply("\x1b[>0;10;0c")
        elif prefix == "?" and final == "n" and first == 6:
            self._reply("\x1b[?{};{}R".format(*screen.position()))
        elif prefix and not (prefix == "?" and final in "JK"):
            pass
        elif final == "c":
            if first == 0:
                self._reply("\x1b[?6c")
        elif final == "n":
            if first == 5:
                self._reply("\x1b[0n")
            elif first == 6:
                self._reply("\x1b[{};{}R".format(*screen.position()))
        elif final == "r":
            screen.set_margins(first or 1, (numbers + [0])[1] or screen.lines)
            if screen.margins == (0, screen.lines - 1):
                screen.margins = None
        elif final == "b":
            if self._last:
                screen.draw(self._last * min(first or 1, screen.columns))
        elif final in ("s", "u"):
            if not body:
                getattr(screen, _CSI[final])()
        elif final == "I":
            for _ in range(min(first or 1, screen.columns)):
                screen.tab()
        elif final in ("H", "f"):
            screen.cursor_position(*numbers[:2])
        elif name := _CSI.get(final):
            getattr(screen, name)(first)

    def _soft_reset(self) -> None:
        screen = self.screen
        screen.mode = {mo.DECAWM, mo.DECTCEM}
        screen.margins = None
        screen.cursor.attrs = screen.default_char
        self.app_cursor = False

    def _mode_state(self, mode: int) -> int:
        """1 set, 2 reset, 0 unknown."""
        states = {
            1: self.app_cursor,
            6: mo.DECOM in self.screen.mode,
            7: mo.DECAWM in self.screen.mode,
            25: self.cursor_visible,
            1004: self.focus_events,
            1049: self.alternate,
            2004: self.bracketed_paste,
            2026: self.synchronized,
        }
        return 0 if mode not in states else 1 if states[mode] else 2

    def _set_mode(self, mode: int, on: bool, private: bool) -> None:
        screen = self.screen
        if not private:
            if mode in (mo.IRM, mo.LNM):
                (screen.set_mode if on else screen.reset_mode)(mode)
        elif mode in (6, 7, 25):
            (screen.set_mode if on else screen.reset_mode)(mode, private=True)
        elif mode == 1:
            self.app_cursor = on
        elif mode == 1004:
            self.focus_events = on
        elif mode == 2004:
            self.bracketed_paste = on
        elif mode == 2026:
            self.synchronized = on
        elif mode == 1048:
            (screen.save_cursor if on else screen.restore_cursor)()
        elif mode in (47, 1047, 1049):
            self._set_alternate(on)

    def _set_alternate(self, on: bool) -> None:
        screen = self.screen
        if on == self.alternate:
            return
        if on:
            self._main = (
                screen.buffer,
                copy.copy(screen.cursor),
                screen.savepoints,
                screen.margins,
            )
            screen.buffer = copy.copy(screen.buffer)  # keeps the default factory
            screen.buffer.clear()
            screen.savepoints = []
        else:
            assert self._main is not None
            screen.buffer, screen.cursor, screen.savepoints, screen.margins = self._main
            self._main = None
        if on:
            screen.margins = None
        screen.keep_history = not on
        screen.dirty.update(range(screen.lines))

    def _style(self, body: str) -> None:
        changes: dict[str, Any] = {}
        parts = body.split(";")
        while parts:
            part = parts.pop(0)
            if ":" in part:
                sub = part.split(":")
                code, values = sub[0], [v for v in sub[1:]]
                if code == "4":
                    changes["underscore"] = values[0] != "0"
                    continue
                if values[:1] == ["2"] and len(values) > 4:
                    del values[1]  # color space
            else:
                code = part
                count = {"5": 2, "2": 4}.get(parts[0] if parts else "", 1)
                values = parts[:count] if code in ("38", "48", "58") else []
                del parts[: len(values)]
            number = int(code[-4:] or 0)
            if number == 0:
                changes = dict(Cell()._asdict())
            elif number in (38, 48):
                color = self._color([int(v[-4:] or 0) for v in values])
                if color is not None:
                    changes["fg" if number == 38 else "bg"] = color
            elif number in _FLAGS:
                changes[_FLAGS[number]] = True
            elif number in _OFF:
                changes |= _OFF[number]
            else:
                for first, key, shift in (
                    (30, "fg", 0),
                    (40, "bg", 0),
                    (90, "fg", 8),
                    (100, "bg", 8),
                ):
                    if first <= number < first + 8:
                        changes[key] = number - first + shift
        cursor = self.screen.cursor
        cursor.attrs = cursor.attrs._replace(**changes)

    @staticmethod
    def _color(values: list[int]) -> int | str | None:
        if len(values) == 2 and values[0] == 5 and values[1] < 256:
            return values[1]
        if len(values) == 4 and values[0] == 2 and max(values) < 256:
            return "#{:02x}{:02x}{:02x}".format(*values[1:])
        return None


# --- input ---

_K = Qt.Key
_M = Qt.KeyboardModifier
_MAC = sys.platform == "darwin"
# Qt swaps the two on macOS.
_CTRL = _M.MetaModifier if _MAC else _M.ControlModifier
_COMMAND = _M.ControlModifier if _MAC else _M.NoModifier
_CURSOR_KEYS = {
    _K.Key_Up: "A",
    _K.Key_Down: "B",
    _K.Key_Right: "C",
    _K.Key_Left: "D",
    _K.Key_Home: "H",
    _K.Key_End: "F",
}
_TILDE_KEYS = {
    _K.Key_Insert: 2,
    _K.Key_Delete: 3,
    _K.Key_PageUp: 5,
    _K.Key_PageDown: 6,
    _K.Key_F5: 15,
    _K.Key_F6: 17,
    _K.Key_F7: 18,
    _K.Key_F8: 19,
    _K.Key_F9: 20,
    _K.Key_F10: 21,
    _K.Key_F11: 23,
    _K.Key_F12: 24,
}
_F_KEYS = {_K.Key_F1: "P", _K.Key_F2: "Q", _K.Key_F3: "R", _K.Key_F4: "S"}


def encode_key(key: int, mods: Qt.KeyboardModifier, text: str, app_cursor: bool = False) -> str:
    """What the key sends to the program; "" for none."""
    shift, alt = bool(mods & _M.ShiftModifier), bool(mods & _M.AltModifier)
    ctrl = bool(mods & _CTRL)
    modifier = 1 + shift + 2 * alt + 4 * ctrl
    escape = "\x1b" if alt else ""
    if letter := _CURSOR_KEYS.get(key):
        if modifier > 1:
            return f"\x1b[1;{modifier}{letter}"
        return ("\x1bO" if app_cursor else "\x1b[") + letter
    if letter := _F_KEYS.get(key):
        return f"\x1b[1;{modifier}{letter}" if modifier > 1 else f"\x1bO{letter}"
    if number := _TILDE_KEYS.get(key):
        return f"\x1b[{number};{modifier}~" if modifier > 1 else f"\x1b[{number}~"
    if key in (_K.Key_Return, _K.Key_Enter):
        return "\n" if ctrl else "\x1b\r" if shift or alt else "\r"
    if key == _K.Key_Backspace:
        return escape + ("\b" if ctrl else "\x7f")
    if key == _K.Key_Backtab or (key == _K.Key_Tab and shift):
        return "\x1b[Z"
    if key == _K.Key_Tab:
        return escape + "\t"
    if key == _K.Key_Escape:
        return escape + "\x1b"
    if ctrl and not alt:
        if key == _K.Key_Space:
            return "\0"
        if 0x40 <= key <= 0x5F:
            return chr(key & 0x1F)
        return "\x1f" if key == _K.Key_Slash else ""
    if text and text.isprintable():
        # AltGr arrives as Ctrl+Alt on Windows; Option on macOS changes the character.
        plain = len(text) == 1 and ord(text.upper()[0]) == key
        return escape + text if alt and not ctrl and plain else text
    if ctrl and 0x40 <= key <= 0x5F:
        return escape + chr(key & 0x1F)
    return ""


def clean_paste(text: str, bracketed: bool) -> str:
    """Without control characters, so that the text cannot act as key presses."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text).replace("\n", "\r")
    return f"\x1b[200~{text}\x1b[201~" if bracketed else text


class TerminalWidget(QWidget):
    """Shows one program. bridge: see ai/pty.PtyBridge."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setAccessibleName("Terminal of the AI coach")
        self._bridge: Any = None
        self._fonts: dict[tuple[bool, bool], QFont] = {}
        self._cell_w, self._cell_h, self._ascent = 8, 16, 12.0
        self._set_font()
        self._colors: dict[Style, tuple[QColor, QColor | None]] = {}
        self.emulator = Emulator(24, 80, self.send)
        self._offset = 0  # lines scrolled back
        self._pushed = 0  # as of the last frame
        self._cursor_row = 0
        self._full = True
        self._held_since = 0.0
        self._selection: tuple[tuple[int, int], tuple[int, int]] | None = None  # anchor, end
        self._selecting = False
        self._wheel = 0
        self._preedit = ""
        self._bar = QScrollBar(Qt.Orientation.Vertical, self)
        self._bar.setCursor(Qt.CursorShape.ArrowCursor)
        self._bar.hide()
        self._bar.valueChanged.connect(self._on_bar)
        self._more = self._timer(self._pump)
        self._frame = self._timer(self._show_frame)
        self._resized = self._timer(self.apply_size)

    def _timer(self, slot: Callable[[], object]) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        timer.timeout.connect(lambda: self._guard(slot))
        return timer

    def _set_font(self) -> None:
        base = QFont()
        base.setFamilies(["Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "monospace"])
        base.setStyleHint(QFont.StyleHint.Monospace)
        base.setFixedPitch(True)
        base.setKerning(False)
        base.setPointSizeF(10)
        metrics = QFontMetricsF(base)
        self._cell_w = max(1, round(metrics.horizontalAdvance("M" * 10) / 10))
        self._cell_h = max(1, round(metrics.height()))
        self._ascent = metrics.ascent()
        for bold in (False, True):
            for italic in (False, True):
                font = QFont(base)
                font.setBold(bold)
                font.setItalic(italic)
                advance = QFontMetricsF(font).horizontalAdvance("M" * 10) / 10
                # exactly on the grid of cells
                font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, self._cell_w - advance)
                self._fonts[bold, italic] = font

    # --- program ---

    def attach(self, bridge: Any) -> None:
        self._bridge = bridge
        bridge.ready_read.connect(lambda: self._guard(self._pump))

    def reset(self) -> None:
        """Empty, for a new program."""
        screen = self.emulator.screen
        self.emulator = Emulator(screen.lines, screen.columns, self.send)
        self._offset = self._pushed = 0
        self._selection = None
        self._preedit = ""
        self._refresh()

    def grid(self) -> tuple[int, int]:
        """Rows and columns of the program."""
        return self.emulator.screen.lines, self.emulator.screen.columns

    def apply_size(self) -> None:
        """At once, instead of some time after the last resize event."""
        self._resized.stop()
        width = self.width() - 2 * PADDING - self._bar.sizeHint().width()
        height = self.height() - 2 * PADDING
        sizes = (height // self._cell_h, width // self._cell_w)
        limits = zip(sizes, MIN_GRID, MAX_GRID, strict=True)
        rows, cols = (max(low, min(n, high)) for n, low, high in limits)
        if (rows, cols) == self.grid():
            return
        self.emulator.resize(rows, cols)
        self._selection = None
        if self._bridge is not None:
            self._bridge.resize(rows, cols)
        self._refresh()

    def send(self, text: str) -> None:
        if self._bridge is not None and text:
            self._bridge.write(text)

    def _pump(self) -> None:
        """Short slices: output must not block the GUI, whatever the program prints."""
        if self._bridge is None:
            return
        deadline = time.perf_counter() + BUDGET_S
        while text := self._bridge.read(SLICE_CHARS):
            self.emulator.feed(text)
            if time.perf_counter() > deadline:
                self._more.start(YIELD_MS)
                break
        self._schedule()

    def _schedule(self) -> None:
        if not self._frame.isActive():
            self._frame.start(FRAME_MS)

    def _show_frame(self) -> None:
        emulator, screen = self.emulator, self.emulator.screen
        if emulator.synchronized:
            now = time.monotonic()
            # since the first frame held back: a busy program is nearly always within an update
            self._held_since = self._held_since or now
            if now < self._held_since + SYNC_S:
                self._frame.start(FRAME_MS)
                return
        self._held_since = 0.0
        pushed = screen.pushed - self._pushed
        self._pushed = screen.pushed
        if self._offset:  # keep showing the same lines
            self._offset = min(self._offset + pushed, emulator.history_size)
        self._sync_bar()
        if self._full or self._offset or pushed:
            self.update()
        else:
            for row in screen.dirty | {self._cursor_row, screen.cursor.y}:
                self.update(self._row_rect(row))
        screen.dirty.clear()
        self._full = False
        self._cursor_row = screen.cursor.y

    def _refresh(self) -> None:
        self._full = True
        self._schedule()

    # --- scrolling ---

    def _sync_bar(self) -> None:
        size = self.emulator.history_size
        self._offset = min(self._offset, size)
        bar = self._bar
        bar.blockSignals(True)
        bar.setRange(0, size)
        bar.setPageStep(self.grid()[0])
        bar.setValue(size - self._offset)
        bar.blockSignals(False)
        bar.setVisible(size > 0)

    def _on_bar(self, value: int) -> None:
        self._offset = max(0, self._bar.maximum() - value)
        self._refresh()

    def scroll(self, lines: int) -> None:  # type: ignore[override]
        """Back in the history (> 0) or forward."""
        offset = max(0, min(self._offset + lines, self.emulator.history_size))
        if offset != self._offset:
            self._offset = offset
            self._refresh()

    def wheelEvent(self, event: QWheelEvent | None) -> None:
        if event is None:
            return
        self._wheel += event.angleDelta().y()
        steps = int(self._wheel / 40)  # 3 lines per notch
        self._wheel -= steps * 40
        if self.emulator.alternate:
            key = _K.Key_Up if steps > 0 else _K.Key_Down
            text = encode_key(key, _M.NoModifier, "", self.emulator.app_cursor)
            self._guard(lambda: self.send(text * abs(steps)))
        else:
            self.scroll(steps)
        event.accept()

    # --- painting ---

    def _row_rect(self, row: int) -> QRect:
        return QRect(0, PADDING + row * self._cell_h, self.width(), self._cell_h)

    def paintEvent(self, event: QPaintEvent | None) -> None:
        painter = QPainter(self)
        try:
            clip = self.rect() if event is None else event.rect()
            painter.fillRect(clip, QColor(BACKGROUND))
            first = max(0, (clip.top() - PADDING) // self._cell_h)
            last = min(self.grid()[0] - 1, (clip.bottom() - PADDING) // self._cell_h)
            for row in range(first, last + 1):
                self._paint_row(painter, row)
            self._paint_cursor(painter)
        except Exception:
            log.debug("painting the terminal failed", exc_info=True)
        finally:
            painter.end()

    def _paint_row(self, painter: QPainter, row: int) -> None:
        number = self.emulator.screen.pushed - self._offset + row
        width, height = self._cell_w, self._cell_h
        top = PADDING + row * height
        columns = self.grid()[1]
        line = [run for run in self.emulator.line(number) if run.x < columns]
        for run in line:
            background = self._style_colors(run.style)[1]
            if background is not None:
                cells = min(run.cells, columns - run.x)
                painter.fillRect(PADDING + run.x * width, top, cells * width, height, background)
        if (selected := self._selected_columns(number)) is not None:
            first, last = selected
            painter.fillRect(
                PADDING + first * width, top, (last - first) * width, height, QColor(theme.SELECTED)
            )
        for run in line:
            if run.text.isspace() and not (run.style[5] or run.style[6]):
                continue
            painter.setPen(self._style_colors(run.style)[0])
            self._draw_text(painter, run.x, top, run.text[: columns - run.x], run.style)

    def _draw_text(self, painter: QPainter, x: int, top: int, text: str, style: Style) -> None:
        _, _, bold, _, italic, underline, strike, _ = style
        font = self._fonts[bold, italic]
        if underline or strike:
            font = QFont(font)
            font.setUnderline(underline)
            font.setStrikeOut(strike)
        painter.setFont(font)
        painter.drawText(QPointF(PADDING + x * self._cell_w, top + self._ascent), text)

    def _style_colors(self, style: Style) -> tuple[QColor, QColor | None]:
        """Of the text and, unless it is the one of the widget, the background."""
        if style not in self._colors:
            if len(self._colors) > 4096:
                self._colors.clear()
            fg, bg, _, dim, *_, reverse = style
            front = QColor(_color_name(fg, FOREGROUND))
            back = QColor(_color_name(bg, BACKGROUND))
            if reverse:
                front, back = back, front
            if dim:
                mixed = zip(_rgb(front), _rgb(back), strict=True)
                front = QColor(*(round(0.6 * a + 0.4 * b) for a, b in mixed))
            plain = back.name() == BACKGROUND.lower()
            self._colors[style] = (front, None if plain else back)
        return self._colors[style]

    def _cursor_rect(self) -> QRect:
        screen = self.emulator.screen
        x = min(screen.cursor.x, screen.columns - 1)
        y = screen.cursor.y
        return QRect(
            PADDING + x * self._cell_w,
            PADDING + (y + self._offset) * self._cell_h,
            self._cell_w,
            self._cell_h,
        )

    def _paint_cursor(self, painter: QPainter) -> None:
        emulator, screen = self.emulator, self.emulator.screen
        running = self._bridge is not None and getattr(self._bridge, "is_running", True)
        row = screen.cursor.y + self._offset
        if not running or row >= screen.lines or not (emulator.cursor_visible or self._preedit):
            return
        rect = self._cursor_rect()
        if self._preedit:
            painter.setFont(self._fonts[False, False])
            wide = sum(max(0, _width(c)) for c in self._preedit)
            box = QRect(rect.left(), rect.top(), wide * self._cell_w, rect.height())
            painter.fillRect(box, QColor(theme.RAISED))
            painter.setPen(QColor(FOREGROUND))
            painter.drawText(QPointF(box.left(), box.top() + self._ascent), self._preedit)
            return
        painter.setPen(QColor(FOREGROUND))
        if not self.hasFocus():
            painter.drawRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5))
            return
        x = min(screen.cursor.x, screen.columns - 1)
        cell = screen.buffer[screen.cursor.y][x]
        painter.fillRect(rect, QColor(FOREGROUND))
        if cell.data.strip():
            painter.setPen(QColor(BACKGROUND))
            self._draw_text(painter, x, rect.top(), cell.data, tuple(cell[1:]))

    # --- selection and clipboard ---

    def _cell_at(self, point: QPointF) -> tuple[int, int]:
        """Absolute line number and the nearest gap between two columns."""
        rows, columns = self.grid()
        row = int((point.y() - PADDING) // self._cell_h)
        column = round((point.x() - PADDING) / self._cell_w)
        row, column = max(0, min(row, rows - 1)), max(0, min(column, columns))
        return self.emulator.screen.pushed - self._offset + row, column

    def _selected_columns(self, number: int) -> tuple[int, int] | None:
        if self._selection is None:
            return None
        (first, start), (last, end) = sorted(self._selection)
        if not first <= number <= last:
            return None
        start = start if number == first else 0
        end = end if number == last else self.grid()[1]
        return (start, end) if end > start else None

    def selected_text(self) -> str:
        if self._selection is None:
            return ""
        (first, _), (last, _) = sorted(self._selection)
        # Lines can be gone from the history since.
        first = max(first, self.emulator.screen.pushed - self.emulator.history_size)
        texts = []
        for number in range(first, last + 1):
            columns = self._selected_columns(number)
            texts.append("" if columns is None else line_text(self.emulator.line(number), *columns))
        return "\n".join(texts)

    def copy(self) -> None:
        text = self.selected_text()
        clipboard = QGuiApplication.clipboard()
        if text.strip() and clipboard is not None:
            clipboard.setText(text)
        self._select(None)

    def paste(self) -> None:
        clipboard = QGuiApplication.clipboard()
        text = "" if clipboard is None else clipboard.text()
        if text:
            self._to_bottom()
            self.send(clean_paste(text, self.emulator.bracketed_paste))

    def _select(self, selection: tuple[tuple[int, int], tuple[int, int]] | None) -> None:
        if selection != self._selection:
            self._selection = selection
            self._refresh()

    def _to_bottom(self) -> None:
        self._select(None)
        self.scroll(-self._offset)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            cell = self._cell_at(event.position())
            self._selecting = True
            self._select((cell, cell))
        elif event.button() == Qt.MouseButton.RightButton:
            self._guard(self.paste)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        if event is None or not self._selecting or self._selection is None:
            return
        y = event.position().y()
        self.scroll(1 if y < 0 else -1 if y > self.height() else 0)
        self._select((self._selection[0], self._cell_at(event.position())))

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        self._selecting = False
        if self._selection is not None and self._selection[0] == self._selection[1]:
            self._select(None)

    def mouseDoubleClickEvent(self, event: QMouseEvent | None) -> None:
        """Selects the word."""
        if event is None or event.button() != Qt.MouseButton.LeftButton:
            return
        number = self._cell_at(event.position())[0]
        column = int((event.position().x() - PADDING) // self._cell_w)
        line = self.emulator.line(number)
        start = end = max(0, column)
        while start > 0 and line_text(line, start - 1, start):
            start -= 1
        while end < self.grid()[1] and line_text(line, end, end + 1):
            end += 1
        self._selecting = False
        self._select(((number, start), (number, end)) if end > start else None)

    def contextMenuEvent(self, event: QContextMenuEvent | None) -> None:
        if event is not None:
            event.accept()  # the right button pastes

    # --- keyboard ---

    def event(self, event: QEvent | None) -> bool:
        # Keys belong to the program, not to the shortcuts of the app.
        if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.ShortcutOverride:
            if not self._leaves(event):
                event.accept()
            return True
        return super().event(event)

    @staticmethod
    def _leaves(event: QKeyEvent) -> bool:
        """Ctrl+Tab goes to the next widget; Alt+F4 belongs to the window."""
        key, mods = event.key(), event.modifiers()
        if key in (_K.Key_Tab, _K.Key_Backtab):
            return bool(mods & _M.ControlModifier)
        return key == _K.Key_F4 and bool(mods & _M.AltModifier)

    def focusNextPrevChild(self, forward: bool) -> bool:
        if self._bridge is not None and getattr(self._bridge, "is_running", True):
            return False  # Tab belongs to the program
        return super().focusNextPrevChild(forward)

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        if event is None:
            return
        if self._leaves(event):
            if event.key() == _K.Key_F4:
                event.ignore()
            else:
                back = event.key() == _K.Key_Backtab or bool(event.modifiers() & _M.ShiftModifier)
                super().focusNextPrevChild(not back)
            return
        event.accept()
        self._guard(lambda: self._key(event))

    def _key(self, event: QKeyEvent) -> None:
        key = event.key()
        mods = event.modifiers() & ~_M.KeypadModifier
        shift = bool(mods & _M.ShiftModifier)
        ctrl, command = bool(mods & _CTRL), bool(mods & _COMMAND)
        altgr = bool(mods & _M.AltModifier)
        if (ctrl or command) and not altgr:
            if key == _K.Key_C and (shift or command or self.selected_text()):
                self.copy()
                return
            if key == _K.Key_V:
                self.paste()
                return
        if command:
            return
        if shift and not ctrl and not altgr:
            if key == _K.Key_Insert:
                self.paste()
                return
            if key in (_K.Key_PageUp, _K.Key_PageDown) and not self.emulator.alternate:
                page = max(1, self.grid()[0] - 1)
                self.scroll(page if key == _K.Key_PageUp else -page)
                return
        text = encode_key(key, mods, event.text(), self.emulator.app_cursor)
        if text:
            self._to_bottom()
            self.send(text)

    def inputMethodEvent(self, event: QInputMethodEvent | None) -> None:
        if event is None:
            return
        self._preedit = event.preeditString()
        text = clean_paste(event.commitString(), False)
        if text:
            self._guard(lambda: self.send(text))
        self._refresh()
        event.accept()

    def inputMethodQuery(self, query: Qt.InputMethodQuery) -> Any:
        if query == Qt.InputMethodQuery.ImEnabled:
            return True
        if query == Qt.InputMethodQuery.ImCursorRectangle:
            return self._cursor_rect()
        return super().inputMethodQuery(query)

    def focusInEvent(self, event: QFocusEvent | None) -> None:
        super().focusInEvent(event)
        self._focus_changed("\x1b[I")

    def focusOutEvent(self, event: QFocusEvent | None) -> None:
        super().focusOutEvent(event)
        self._focus_changed("\x1b[O")

    def _focus_changed(self, report: str) -> None:
        if self.emulator.focus_events:
            self._guard(lambda: self.send(report))
        self._refresh()

    # --- layout ---

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        super().resizeEvent(event)
        width = self._bar.sizeHint().width()
        self._bar.setGeometry(self.width() - width, 0, width, self.height())
        self._resized.start(RESIZE_MS)

    def _guard(self, action: Callable[[], object]) -> None:
        """Never let a failure escape a slot: PyQt6 would abort the app."""
        try:
            action()
        except Exception:
            log.exception("terminal failed")


def _color_name(color: int | str, default: str) -> str:
    if isinstance(color, int):
        return PALETTE[color]
    return color or default


def _rgb(color: QColor) -> tuple[int, int, int]:
    return color.red(), color.green(), color.blue()
