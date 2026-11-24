import random
import time

import pytest
from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QGuiApplication,
    QInputMethodEvent,
    QKeyEvent,
    QKeySequence,
    QShortcut,
    QWheelEvent,
)
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

from pywatts.ui import theme
from pywatts.ui.widgets import terminal
from pywatts.ui.widgets.terminal import (
    PADDING,
    Emulator,
    TerminalWidget,
    clean_paste,
    encode_key,
    line_text,
)

K, M = Qt.Key, Qt.KeyboardModifier
NONE, SHIFT, CTRL, ALT = M.NoModifier, M.ShiftModifier, M.ControlModifier, M.AltModifier


@pytest.fixture
def emu():
    replies = []
    emulator = Emulator(5, 20, replies.append)
    emulator.replies = replies
    return emulator


def texts(emulator):
    screen = emulator.screen
    return [line_text(emulator.line(screen.pushed + y)) for y in range(screen.lines)]


def state(emulator):
    s = emulator.screen
    rows = [emulator.line(s.pushed + y) for y in range(s.lines)]
    modes = (emulator.app_cursor, emulator.bracketed_paste, emulator.alternate, emulator.title)
    return rows, list(s.history), s.cursor.x, s.cursor.y, tuple(s.cursor.attrs), modes


# --- emulator ---

SEQUENCES = (
    "a\x1b[>4mb\x1b[>0q\x1b[<u\x1b[?u\x1b[?2031h\x1b[?9001h\x1b[?1004h"
    "\x1b]0;title\x07c\x1b]11;?\x1b\\d\x1b]7501;x=1\x07\x1b]52;c;aGFsbG8=\x07e"
    "\x1bP+q544e\x1b\\f\x1b_Gi=1;AAAA\x1b\\\x1b^pm\x1b\\\x1bXsos\x1b\\g"
    "\x1b[38;2;1;2;3mh\x1b[38:2::4:5:6mi\x1b[4:3mj\x1b[0;1;31mk\x1b(0q\x1b(B"
    "日本\x1b[?1h\x1b[?2004h\r\nl\x1b[2;5Hm\x1b[!p\x1b[?2026$p\x1b[6n\x1b[cn😀́‍o️p"
)


def test_filters_unknown_sequences(emu):
    emu.feed(SEQUENCES)
    assert texts(emu)[0].startswith("abcdefghijk─日本")
    assert texts(emu)[1] == "l   mn😀́‍o️p"
    style = emu.line(0)[0].style
    assert style == terminal.PLAIN  # \x1b[>4m is no underline
    assert emu.title == "title"
    assert emu.focus_events and emu.bracketed_paste
    assert emu.replies == [
        "\x1b]11;rgb:1212/1515/1a1a\x1b\\",
        "\x1b[?2026;2$y",
        "\x1b[2;6R",
        "\x1b[?6c",
    ]


def test_split_at_every_position(emu):
    emu.feed(SEQUENCES)
    expected = state(emu), emu.replies
    for i in range(1, len(SEQUENCES)):
        replies = []
        split = Emulator(5, 20, replies.append)
        split.feed(SEQUENCES[:i])
        split.feed(SEQUENCES[i:])
        assert (state(split), replies) == expected, i
    replies = []
    single = Emulator(5, 20, replies.append)
    for char in SEQUENCES:
        single.feed(char)
    assert (state(single), replies) == expected


def test_colors_and_styles(emu):
    emu.feed("\x1b[31ma\x1b[0;92mb\x1b[38;5;200mc\x1b[48;2;1;2;3md\x1b[0;2;3;9;7me\x1b[58;5;1mf")
    runs = emu.line(0)
    assert [r.style[0] for r in runs[:3]] == [1, 10, 200]
    assert runs[3].style[:2] == (200, "#010203")
    assert runs[4].text == "ef"
    assert runs[4].style == ("", "", False, True, True, False, True, True)


@pytest.mark.parametrize(
    "code", ["38;5", "38;5;999", "38;2;1;2", "38;2;999;0;0", "38", "38:9:1", "4:", ";;", "9999999"]
)
def test_bad_colors(emu, code):
    emu.feed(f"\x1b[{code}mx")
    assert texts(emu)[0] == "x"


def test_never_raises():
    rng = random.Random(1)
    parts = ["\x1b", "[", "]", "?", ">", ";", ":", "$", "5", "99999999999", "\x07", "\n", "\r", "ü"]
    parts += ["日", "́", "\x9b", "\x9d", "\x18", "\0", "(0", "#8", "\ud800", "\U0001f468‍"]
    parts += [
        f"\x1b[{n}{c}" for n in ("", "0", "3;9", "9999") for c in "@ABCDEFGHIJKLMPSTXZ`abdefg"
    ]
    parts += [f"\x1b[?{m}{c}" for m in (1, 3, 5, 6, 7, 25, 47, 1048, 1049, 2026) for c in "hl"]
    parts += ["\x1b[2;3r", "\x1b[r", "\x1b7", "\x1b8", "\x1bM", "\x1bD", "\x1bE", "\x1bc", "abc"]
    emulator = Emulator(5, 20, lambda _t: None)
    for i in range(3000):
        emulator.feed("".join(rng.choices(parts, k=8)))
        if i % 10 == 0:
            emulator.resize(rng.randint(2, 9), rng.randint(10, 30))
        screen = emulator.screen
        assert 0 <= screen.cursor.y < screen.lines and 0 <= screen.cursor.x <= screen.columns
        for y in range(screen.lines):
            assert len(line_text(emulator.line(screen.pushed + y))) < 10 * screen.columns
        assert len(screen.savepoints) <= 1


def test_strings_are_bounded(emu):
    emu.feed("\x1b]52;c;")
    for _ in range(100):
        emu.feed("A" * 1000)
    assert len(emu._body) < 1000
    emu.feed("visible")
    assert "visible" in "".join(texts(emu))
    emu.feed("\x1bc\x1b[" + "1;" * 5000)  # a sequence without end
    emu.feed("\r\nnext")
    assert "next" in "".join(texts(emu))
    assert len(emu._pending) <= terminal.SEQUENCE_CHARS


def test_aborted_string_continues_with_the_next_sequence(emu):
    emu.feed("\x1b]0;never ended\x1b[31mred\x18")
    assert emu.line(0)[0][1:3] == ("red", (1, *terminal.PLAIN[1:]))


def test_wrap_scroll_and_history(emu):
    emu.feed("x" * 25 + "\r\n" + "\r\n".join(str(i) for i in range(8)))
    assert texts(emu) == ["3", "4", "5", "6", "7"]
    assert emu.screen.pushed == emu.history_size == 5
    assert line_text(emu.line(0)) == "x" * 20
    assert line_text(emu.line(1)) == "x" * 5
    assert emu.line(99) == ()
    emu.feed("\x1b[3J")
    assert emu.history_size == 0 and texts(emu)[4] == "7"


def test_wide_characters(emu):
    emu.feed("x" * 19 + "日y")
    assert texts(emu)[:2] == ["x" * 19, "日y"]
    assert [r.cells for r in emu.line(1)] == [2, 1]
    assert emu.line(1)[1].x == 2
    emu.feed("\x1b[Há️b")  # unprintable characters do not hide what follows
    assert texts(emu)[0].startswith("á️b")


def test_scroll_region(emu):
    emu.feed("a\r\nb\r\nc\r\nd\r\ne\x1b[2;4r\x1b[4;1H\n")
    assert texts(emu) == ["a", "c", "d", "", "e"]
    assert emu.history_size == 0
    emu.feed("\x1b[2;1H\x1bM")
    assert texts(emu) == ["a", "", "c", "d", "e"]
    emu.feed("\x1b[3;1H\x1b[M")
    assert texts(emu) == ["a", "", "d", "", "e"]
    emu.feed("\x1b[r\x1b[2S")
    assert texts(emu) == ["d", "", "e", "", ""]
    assert emu.history_size == 2


def test_editing_a_line(emu):
    emu.feed("abcdef\x1b[1;3H\x1b[2P")
    assert texts(emu)[0] == "abef"
    emu.feed("\x1b[@X")
    assert texts(emu)[0] == "abXef"
    emu.feed("\x1b[2X")
    assert texts(emu)[0] == "abX"
    emu.feed("\x1b[1;2H\x1b[1K")
    assert texts(emu)[0] == "  X"
    emu.feed("\x1b[1;4H-\x1b[5b")
    assert texts(emu)[0] == "  X------"


def test_erase_keeps_only_the_background(emu):
    emu.feed("\x1b[4;7;41m\x1b[2J")
    style = emu.line(0)[0].style
    assert style == ("", 1, False, False, False, False, False, False)
    assert emu.line(0)[0].cells == 20


def test_alternate_screen(emu):
    emu.feed("one\r\ntwo\x1b[31m\x1b[?1049h\x1b[Halt" + "\r\nx" * 10)
    assert emu.alternate and emu.history_size == 0
    assert emu.line(emu.screen.pushed - 1) == ()
    emu.resize(4, 15)
    emu.feed("\x1b[?1049h")  # twice
    emu.feed("\x1b[?1049l")
    assert texts(emu) == ["one", "two", "", ""]
    assert (emu.screen.cursor.x, emu.screen.cursor.y) == (3, 1)
    emu.feed("\x1b[?1049h\x1bc")
    assert not emu.alternate and texts(emu) == [""] * 4


def test_resize_keeps_the_end(emu):
    emu.feed("a\r\nb\r\nc\r\nd\r\ne")
    emu.resize(3, 10)
    assert texts(emu) == ["c", "d", "e"] and emu.history_size == 2
    emu.resize(6, 30)
    emu.feed("f\r\n" + "y" * 30)
    assert texts(emu)[:4] == ["c", "d", "ef", "y" * 30]


def test_saved_cursor_can_be_restored_twice(emu):
    emu.feed("\x1b[2;3H\x1b7\x1b[H\x1b8")
    assert emu.screen.position() == (2, 3)
    emu.feed("\x1b[5;5Hx\x1b8")
    assert emu.screen.position() == (2, 3)


def test_worst_slices_are_fast():
    """A slice of the hardest sequences stays far below the 1 s of the ride tick."""
    emulator = Emulator(60, 200, lambda _t: None)
    emulator.feed("\x1b[41m" + "x" * 20_000)
    for hard in ("\n", "\x1b[L", "\x1b[99S", "\x1b[@", "\x1b[2J", "\x1b#8", "\x1b[9999b", "\x1bc"):
        hard = "\x1b[41my" + hard if hard == "\x1b[9999b" else hard
        start = time.perf_counter()
        emulator.feed(hard * (terminal.SLICE_CHARS // len(hard)))
        assert time.perf_counter() - start < 0.3, hard


# --- keys ---


@pytest.mark.parametrize(
    ("key", "mods", "text", "sent"),
    [
        (K.Key_A, NONE, "a", "a"),
        (K.Key_A, SHIFT, "A", "A"),
        (K.Key_Adiaeresis, NONE, "ä", "ä"),
        (K.Key_Up, NONE, "", "\x1b[A"),
        (K.Key_Left, CTRL, "", "\x1b[1;5D"),
        (K.Key_Right, SHIFT | ALT, "", "\x1b[1;4C"),
        (K.Key_Home, NONE, "", "\x1b[H"),
        (K.Key_End, NONE, "", "\x1b[F"),
        (K.Key_Insert, NONE, "", "\x1b[2~"),
        (K.Key_Delete, NONE, "\x7f", "\x1b[3~"),
        (K.Key_PageUp, NONE, "", "\x1b[5~"),
        (K.Key_PageDown, CTRL, "", "\x1b[6;5~"),
        (K.Key_F1, NONE, "", "\x1bOP"),
        (K.Key_F4, SHIFT, "", "\x1b[1;2S"),
        (K.Key_F5, NONE, "", "\x1b[15~"),
        (K.Key_F12, NONE, "", "\x1b[24~"),
        (K.Key_Return, NONE, "\r", "\r"),
        (K.Key_Enter, M.KeypadModifier, "\r", "\r"),
        (K.Key_Return, SHIFT, "\r", "\x1b\r"),
        (K.Key_Return, ALT, "\r", "\x1b\r"),
        (K.Key_Backspace, NONE, "\b", "\x7f"),
        (K.Key_Backspace, CTRL, "\x7f", "\b"),
        (K.Key_Backspace, ALT, "\b", "\x1b\x7f"),
        (K.Key_Tab, NONE, "\t", "\t"),
        (K.Key_Backtab, SHIFT, "", "\x1b[Z"),
        (K.Key_Escape, NONE, "\x1b", "\x1b"),
        (K.Key_C, CTRL, "\x03", "\x03"),
        (K.Key_BracketLeft, CTRL, "", "\x1b"),
        (K.Key_Space, CTRL, " ", "\0"),
        (K.Key_Slash, CTRL, "/", "\x1f"),
        (K.Key_1, CTRL, "1", ""),
        (K.Key_B, ALT, "b", "\x1bb"),
        (K.Key_C, CTRL | ALT, "", "\x1b\x03"),
        # AltGr, German layout: Ctrl+Alt on Windows, nothing or GroupSwitch on Linux
        (K.Key_At, CTRL | ALT, "@", "@"),
        (K.Key_Q, CTRL | ALT, "@", "@"),
        (K.Key_BraceLeft, CTRL | ALT, "{", "{"),
        (K.Key_BracketLeft, CTRL | ALT, "[", "["),
        (K.Key_Backslash, CTRL | ALT, "\\", "\\"),
        (K.Key_E, CTRL | ALT, "€", "€"),
        (K.Key_At, M.GroupSwitchModifier, "@", "@"),
        (K.Key_L, ALT, "@", "@"),  # Option on macOS
        (K.Key_Control, CTRL, "", ""),
        (K.Key_AltGr, NONE, "", ""),
    ],
)
def test_keys(key, mods, text, sent):
    assert encode_key(key, mods, text) == sent


def test_application_cursor_keys():
    assert encode_key(K.Key_Up, NONE, "", app_cursor=True) == "\x1bOA"
    assert encode_key(K.Key_Up, CTRL, "", app_cursor=True) == "\x1b[1;5A"


def test_paste_cannot_inject():
    text = "a\r\nb\tc\x1b[201~\x03\x7f\x9b1m\0d\ne"
    assert clean_paste(text, False) == "a\rb\tc[201~1md\re"
    assert clean_paste(text, True) == "\x1b[200~a\rb\tc[201~1md\re\x1b[201~"


# --- widget ---


class FakeBridge(QObject):
    ready_read = pyqtSignal()
    is_running = True

    def __init__(self):
        super().__init__()
        self.pending, self.sent, self.sizes = "", "", []

    def output(self, text):
        self.pending += text
        self.ready_read.emit()

    def read(self, max_chars):
        text, self.pending = self.pending[:max_chars], self.pending[max_chars:]
        return text

    def write(self, text):
        self.sent += text

    def resize(self, rows, cols):
        self.sizes.append((rows, cols))


@pytest.fixture
def bridge(qtbot):
    return FakeBridge()


@pytest.fixture
def term(qtbot, bridge):
    widget = TerminalWidget()
    qtbot.addWidget(widget)
    widget.attach(bridge)
    widget.resize(600, 300)
    widget.show()
    qtbot.waitExposed(widget)
    widget.apply_size()
    widget.setFocus()
    qtbot.waitUntil(widget.hasFocus)
    QGuiApplication.clipboard().clear()
    bridge.sent = ""
    return widget


def point(term, row, column):
    return QPoint(
        PADDING + round(column * term._cell_w), PADDING + row * term._cell_h + term._cell_h // 2
    )


def drag(term, start, end):
    QTest.mousePress(term, Qt.MouseButton.LeftButton, NONE, point(term, *start))
    QTest.mouseMove(term, point(term, *end))
    event = point(term, *end)
    QTest.mouseRelease(term, Qt.MouseButton.LeftButton, NONE, event)


def press(term, key, mods=NONE, text=""):
    event = QKeyEvent(QEvent.Type.KeyPress, key, mods, text)
    QGuiApplication.sendEvent(term, event)
    return event


def test_shows_output(term, bridge, qtbot):
    bridge.output("hello \x1b[31mred\x1b[0m \x1b[7mrev\x1b[0m ─日😀 \x1b[4;9;1;3mx")
    qtbot.wait(60)
    assert term.grid()[0] > 10
    assert line_text(term.emulator.line(0)).startswith("hello red rev")
    image = term.grab().toImage()
    ratio = image.devicePixelRatio()
    x = PADDING + 11 * term._cell_w + 1  # reversed
    pixel = image.pixelColor(round(x * ratio), round((PADDING + 1) * ratio))
    assert pixel.name() == theme.TEXT
    assert image.pixelColor(1, 1).name() == theme.WINDOW


def test_repaints_are_limited(term, bridge, qtbot, monkeypatch):
    paints = []
    original = term.paintEvent

    def paint(event):
        paints.append(1)
        original(event)

    monkeypatch.setattr(term, "paintEvent", paint)
    for _ in range(100):
        bridge.output("x\r\n")
        qtbot.wait(2)
    assert 1 <= len(paints) < 60


def test_synchronized_output_is_held_back(term, bridge, qtbot):
    qtbot.wait(60)
    bridge.output("\x1b[?2026habc")
    qtbot.wait(60)
    assert term._held_since and term._frame.isActive()
    bridge.output("\x1b[?2026l")
    qtbot.waitUntil(lambda: not term._frame.isActive())
    assert not term._held_since
    bridge.output("\x1b[?2026hnever released")
    qtbot.waitUntil(lambda: not term._frame.isActive(), timeout=1000)


def test_synchronized_output_does_not_starve(term, bridge, qtbot):
    """Always within an update when the frame is due."""
    frames = []
    update = term.update
    term.update = lambda *a: (frames.append(1), update(*a))
    end = time.monotonic() + 0.8
    bridge.output("\x1b[?2026h")
    while time.monotonic() < end:
        bridge.output("\x1b[?2026l\x1b[?2026hx\r\n")
        qtbot.wait(3)
    assert len(frames) >= 2


def test_flood_does_not_block(term, qtbot, bridge):
    bridge.output("\x1b[31mline of the flood 日本\x1b[0m \x1b[2K\r\n" * 20_000)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(50)
    qtbot.wait(1000)
    timer.stop()
    assert len(ticks) >= 8
    assert max(b - a for a, b in zip(ticks, ticks[1:], strict=False)) < 0.25
    bridge.pending = ""


def test_corrupt_output_and_failing_bridge(term, bridge, qtbot):
    bridge.output("\x1b[38;5;999m\x1b[;;;;r\x1b[?6h\x1b[9999;9999H\x1b[-1mok")
    qtbot.wait(60)

    def fail(_n):
        raise RuntimeError("deleted")

    bridge.read = fail
    bridge.ready_read.emit()


def test_resize_is_sent_once(term, bridge, qtbot):
    bridge.sizes.clear()
    for width in (500, 480, 460, 420):
        term.resize(width, 200)
        qtbot.wait(5)
    qtbot.waitUntil(lambda: bool(bridge.sizes))
    assert bridge.sizes == [term.grid()]
    assert term.grid()[1] < 420 / term._cell_w
    term.resize(5, 5)
    term.apply_size()
    assert term.grid() == terminal.MIN_GRID


def test_typing(term, bridge):
    QTest.keyClicks(term, "hi")
    QTest.keyClick(term, K.Key_Tab)
    QTest.keyClick(term, K.Key_Backtab, SHIFT)
    QTest.keyClick(term, K.Key_Up)
    bridge.output("\x1b[?1h")
    QTest.keyClick(term, K.Key_Up)
    press(term, K.Key_At, CTRL | ALT, "@")
    press(term, K.Key_V, CTRL | ALT, "")  # not paste
    assert bridge.sent == "hi\t\x1b[Z\x1b[A\x1bOA@\x1b\x16"
    assert term.hasFocus()


def test_ime(term, bridge):
    event = QInputMethodEvent("に", [])
    QGuiApplication.sendEvent(term, event)
    assert bridge.sent == "" and term._preedit == "に"
    term.grab()
    event = QInputMethodEvent("", [])
    event.setCommitString("日\x1b")
    QGuiApplication.sendEvent(term, event)
    assert bridge.sent == "日" and term._preedit == ""
    assert term.inputMethodQuery(Qt.InputMethodQuery.ImEnabled)
    assert not term.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle).isEmpty()


def test_shortcuts_of_the_app_do_not_fire(qtbot, bridge):
    page = QWidget()
    qtbot.addWidget(page)
    term, other = TerminalWidget(), QLineEdit()
    layout = QVBoxLayout(page)
    layout.addWidget(term)
    layout.addWidget(other)
    term.attach(bridge)
    fired = []
    for keys in ("Ctrl+B", "Ctrl+1", "Space", "Esc"):
        QShortcut(QKeySequence(keys), page, activated=lambda k=keys: fired.append(k))
    page.show()
    qtbot.waitExposed(page)
    page.activateWindow()
    term.setFocus()
    qtbot.waitUntil(term.hasFocus)
    for key, mods in ((K.Key_B, CTRL), (K.Key_1, CTRL), (K.Key_Space, NONE), (K.Key_Escape, NONE)):
        QTest.keyClick(term, key, mods)
    assert fired == []
    assert bridge.sent == "\x02 \x1b"
    QTest.keyClick(term, K.Key_Tab, CTRL)
    assert other.hasFocus()
    QTest.keyClick(other, K.Key_B, CTRL)
    assert fired == ["Ctrl+B"]
    term.setFocus()
    bridge.is_running = False  # Tab is free again
    QTest.keyClick(term, K.Key_Tab)
    assert other.hasFocus()


def test_select_and_copy(term, bridge, qtbot):
    bridge.output("first line\r\n日本 wide\r\nlast")
    drag(term, (0, 6), (2, 2))
    assert term.selected_text() == "line\n日本 wide\nla"
    term.grab()
    QTest.keyClick(term, K.Key_C, CTRL)
    assert QGuiApplication.clipboard().text() == "line\n日本 wide\nla"
    assert bridge.sent == ""
    QTest.keyClick(term, K.Key_C, CTRL)  # no selection left
    assert bridge.sent == "\x03"
    drag(term, (2, 4), (2, 0))  # backwards
    QTest.keyClick(term, K.Key_C, CTRL | SHIFT)
    assert QGuiApplication.clipboard().text() == "last"
    QTest.keyClick(term, K.Key_C, CTRL | SHIFT)
    assert QGuiApplication.clipboard().text() == "last"
    assert bridge.sent == "\x03"
    QTest.mouseDClick(term, Qt.MouseButton.LeftButton, NONE, point(term, 0, 7.5))
    assert term.selected_text() == "line"
    QTest.mouseClick(term, Qt.MouseButton.LeftButton, NONE, point(term, 0, 1))
    assert term.selected_text() == ""


def test_selection_stays_on_its_text(term, bridge, qtbot):
    bridge.output("\r\n".join(f"line {i}" for i in range(5)))
    drag(term, (1, 0), (1, 6))
    bridge.output("\r\n" * 100)
    qtbot.wait(50)
    assert term.selected_text() == "line 1"
    bridge.output("\x1b[3J")
    assert term.selected_text() == ""


def test_paste(term, bridge):
    clipboard = QGuiApplication.clipboard()
    clipboard.setText("a\nb\x1b[31m")
    QTest.keyClick(term, K.Key_V, CTRL)
    bridge.output("\x1b[?2004h")
    QTest.keyClick(term, K.Key_V, CTRL | SHIFT)
    QTest.mouseClick(term, Qt.MouseButton.RightButton, NONE, point(term, 0, 0))
    bracketed = "\x1b[200~a\rb[31m\x1b[201~"
    assert bridge.sent == "a\rb[31m" + bracketed * 2


def test_clipboard_is_not_set_by_output(term, bridge, qtbot):
    QGuiApplication.clipboard().setText("mine")
    bridge.output("\x1b]52;c;aGFsbG8=\x07\x1b]52;c;?\x07\x1b]0;new title\x07")
    qtbot.wait(50)
    assert QGuiApplication.clipboard().text() == "mine"
    assert bridge.sent == "" and term.windowTitle() == ""


def wheel(term, delta):
    at = QPointF(50, 50)
    event = QWheelEvent(
        at,
        term.mapToGlobal(at),
        QPoint(),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        NONE,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QGuiApplication.sendEvent(term, event)


def test_scrollback(term, bridge, qtbot):
    bridge.output("".join(f"line {i}\r\n" for i in range(100)))
    qtbot.waitUntil(term._bar.isVisible)
    rows = term.grid()[0]
    assert term._bar.maximum() == 101 - rows
    wheel(term, 120)
    assert term._offset == 3
    qtbot.wait(50)
    assert term._bar.value() == term._bar.maximum() - 3
    bridge.output("more\r\n" * 2)  # the shown lines stay
    qtbot.wait(50)
    assert term._offset == 5
    term._bar.setValue(0)
    assert term._offset == term.emulator.history_size
    term.grab()
    QTest.keyClick(term, K.Key_A)
    assert term._offset == 0
    QTest.keyClick(term, K.Key_PageUp, SHIFT)
    assert term._offset == rows - 1
    QTest.keyClick(term, K.Key_PageDown, SHIFT)
    assert term._offset == 0 and bridge.sent == "a"
    bridge.output("\x1b[?1049h")
    qtbot.waitUntil(lambda: not term._bar.isVisible())
    wheel(term, -120)
    assert bridge.sent == "a" + "\x1b[B" * 3


def test_scrollback_is_bounded(term, bridge, qtbot, monkeypatch):
    emulator = term.emulator
    emulator.feed("x\r\n" * (terminal.SCROLLBACK + 500))
    assert emulator.history_size == terminal.SCROLLBACK
    term.scroll(10_000)
    assert term._offset == terminal.SCROLLBACK
    emulator.feed("x\r\n" * 10)
    term._show_frame()
    term.grab()


def test_focus_reports(term, bridge):
    term.clearFocus()
    assert bridge.sent == ""
    bridge.output("\x1b[?1004h")
    term.setFocus()
    term.clearFocus()
    assert bridge.sent == "\x1b[I\x1b[O"


def test_cursor(term, bridge, qtbot):
    qtbot.wait(50)

    def shown():
        image = term.grab().toImage()
        ratio = image.devicePixelRatio()
        at = term._cursor_rect().center()
        return image.pixelColor(round(at.x() * ratio), round(at.y() * ratio)).name() == theme.TEXT

    assert shown()
    bridge.output("\x1b[?25l")
    assert not shown()
    bridge.output("\x1b[?25h")
    term.clearFocus()  # outline
    assert not shown()


def test_reset(term, bridge, qtbot):
    bridge.output("x\r\n" * 100 + "\x1b[?2004h\x1b]0;")
    size = term.grid()
    term.reset()
    assert term.grid() == size
    assert term.emulator.history_size == 0 and not term.emulator.bracketed_paste
    bridge.output("new")
    assert line_text(term.emulator.line(0)) == "new"
