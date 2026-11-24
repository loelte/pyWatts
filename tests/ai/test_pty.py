import gc
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QObject, QTimer

from pywatts.ai import pty
from pywatts.ai.pty import PtyBridge, child_env
from pywatts.sync import gui_gc

WINDOWS = sys.platform == "win32"
RAW = (
    "import msvcrt, sys\ndef get(): return msvcrt.getwch()\n"
    if WINDOWS
    else "import sys, tty\ntty.setraw(0)\ndef get(): return sys.stdin.read(1)\n"
)


class Program:
    """A Python child in a bridge; collects its output."""

    def __init__(self, qtbot, bridge):
        self.qtbot, self.bridge = qtbot, bridge
        self.output, self.codes, self.errors = "", [], []
        bridge.ready_read.connect(self._read)
        bridge.finished.connect(self.codes.append)
        bridge.failed.connect(self.errors.append)

    def _read(self):
        while text := self.bridge.read(1000):
            self.output += text

    def run(self, code, *args, env=None, cwd=None, size=(24, 200)):
        argv = [sys.executable, "-X", "utf8", "-c", code, *args]
        self.bridge.start(argv, cwd or Path.cwd(), env or {}, *size)

    def wait(self, text):
        self.qtbot.waitUntil(lambda: text in self.output, timeout=10_000)

    def wait_end(self):
        self.qtbot.waitUntil(lambda: bool(self.codes), timeout=10_000)
        return self.codes[-1]


@pytest.fixture
def program(qtbot):
    bridge = PtyBridge()
    yield Program(qtbot, bridge)
    bridge.stop()
    assert gc.isenabled() and gui_gc._users == 0


def alive(pid):
    if WINDOWS:
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True
        ).stdout
        return str(pid).encode() in listing
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_output_and_exit_code(program):
    program.run("import sys; print('hällo € 日本 😀'); sys.exit(7)")
    assert program.wait_end() == 7
    program.wait("hällo € 日本 😀")
    assert not program.bridge.is_running
    assert not program.errors


def test_arguments_arrive_unchanged(program):
    args = ["", "a b", 'q"uo\\"te', "dir\\", "dir x\\", "n1\nn2", "ü€日", "%PATH%", "a&b|c^", "-"]
    program.run("import json, sys; print(json.dumps(sys.argv[1:]), end='|')", *args, size=(24, 400))
    program.wait_end()
    program.wait("]|")
    text = re.search(r"\[\"\".*\](?=\|)", program.output).group()
    assert json.loads(text) == args


def test_environment_and_directory(program, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("ANTHROPIC_MODEL", "keep")
    folder = tmp_path / "dür"
    folder.mkdir()
    program.run(
        "import os; e = os.environ; print(os.path.basename(os.getcwd()), e['PYW'], e['TERM'], "
        "e.get('CLAUDECODE'), e.get('CLAUDE_CODE_ENTRYPOINT'), e.get('ANTHROPIC_MODEL'))",
        env={"PYW": "wört"},
        cwd=folder,
    )
    program.wait("dür wört xterm-256color None None keep")
    assert child_env({"TERM": "dumb"})["TERM"] == "dumb"


def test_input_and_resize(program):
    program.run(
        RAW + "import os\nprint('ready', flush=True)\n"
        "while (c := get()) != 'q': print(c.upper(), os.get_terminal_size(), flush=True)",
        size=(20, 90),
    )
    program.wait("ready")
    program.bridge.write("ö")
    program.wait("Ö os.terminal_size(columns=90, lines=20)")
    program.bridge.resize(30, 120)
    program.bridge.write("x")
    program.wait("X os.terminal_size(columns=120, lines=30)")
    program.bridge.write("\ud800q")  # a lone surrogate is dropped
    assert program.wait_end() == 0


def test_long_input_arrives(program):
    program.run(
        RAW + "print('ready', flush=True)\nn = 0\nwhile get() != 'q': n += 1\nprint('got', n)"
    )
    program.wait("ready")
    program.bridge.write("ä" * 10_000 + "q")
    program.wait("got 10000")


def test_ctrl_c(program):
    # A test runner can have turned Ctrl+C off for its children.
    on = "import ctypes; ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)\n"
    program.run((on if WINDOWS else "") + "import time\nprint('ready', flush=True)\ntime.sleep(30)")
    program.wait("ready")
    program.bridge.write("\x03")
    assert program.wait_end() != 0
    program.wait("KeyboardInterrupt")


CHILDREN = (
    "import subprocess, sys, time\n"
    "q = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print('pid', q.pid, 'end', flush=True)\n"
)


@pytest.mark.parametrize("last", ["time.sleep(60)", "pass"])
def test_grandchildren_end(program, qtbot, last):
    """Also if the program itself ended before."""
    program.run(CHILDREN + last)
    program.wait(" end")
    pid = int(re.search(r"pid (\d+) end", program.output).group(1))
    if last == "pass":
        program.wait_end()
    else:
        assert alive(pid)
        program.bridge.stop()
        program.bridge.stop()
    qtbot.waitUntil(lambda: not alive(pid), timeout=5000)
    assert not program.bridge.is_running


def test_stop_is_silent_and_fast(program, qtbot):
    program.run("import time; time.sleep(60)")
    qtbot.wait(200)
    start = time.monotonic()
    program.bridge.stop()
    assert time.monotonic() - start < 1.5
    qtbot.wait(50)
    assert program.codes == []
    assert program.bridge.read(10) == ""
    program.bridge.write("x")
    program.bridge.resize(10, 20)


def test_stop_right_after_start(program, qtbot):
    for _ in range(3):
        program.run("print(1)")
        program.bridge.stop()
    qtbot.wait(50)
    assert program.codes == [] and program.output == ""


def test_start_again_ends_the_first(program):
    program.run("import time; print('one', flush=True); time.sleep(60)")
    program.wait("one")
    program.run("print('two')")
    assert program.wait_end() == 0
    program.wait("two")
    assert len(program.codes) == 1


def test_flood_is_held_back(qtbot):
    """The program waits while nobody reads, and nothing is lost."""
    bridge = PtyBridge()
    try:
        code = "for i in range(12000): print(i, 'x' * 60)\nprint('last')"
        bridge.start([sys.executable, "-c", code], Path.cwd(), {}, 24, 200)
        session = bridge._session
        qtbot.waitUntil(lambda: session._size >= pty.BUFFER_CHARS, timeout=10_000)
        qtbot.wait(300)
        assert session._size < pty.BUFFER_CHARS + 100_000
        assert bridge.is_running
        output = ""

        def ended():
            nonlocal output
            while text := bridge.read(65536):
                output += text
            return not bridge.is_running and "last" in output

        qtbot.waitUntil(ended, timeout=15_000)
        numbers = [int(n) for n in re.findall(r"(\d+) x{60}", output)]
        assert numbers == list(range(12000))
    finally:
        bridge.stop()


def test_stop_while_the_reader_waits(qtbot):
    bridge = PtyBridge()
    bridge.start([sys.executable, "-c", "while True: print('x' * 70)"], Path.cwd(), {}, 24, 80)
    session = bridge._session
    qtbot.waitUntil(lambda: session._size >= pty.BUFFER_CHARS, timeout=10_000)
    start = time.monotonic()
    bridge.stop()
    assert time.monotonic() - start < 1.5
    assert not session._thread.is_alive()


def test_writing_never_blocks(program, qtbot):
    program.run("import time; print('ready', flush=True); time.sleep(60)")
    program.wait("ready")
    start = time.monotonic()
    for _ in range(10):
        program.bridge.write("x" * 1_000_000)  # beyond the limit it is dropped
    assert time.monotonic() - start < 0.5
    start = time.monotonic()
    program.bridge.stop()
    assert time.monotonic() - start < 1.9
    qtbot.waitUntil(lambda: not [t for t in threading.enumerate() if t.name.startswith("pty")])


def test_start_error(program):
    program.bridge.start(["/no/such/program.exe", "x"], Path.cwd(), {}, 24, 80)
    code = program.wait_end()
    if WINDOWS:
        assert code is None and "program.exe" in program.errors[0]
    with pytest.raises(ValueError):
        program.bridge.start([], Path.cwd(), {}, 24, 80)


def test_deleted_without_stop(qtbot):
    parent = QObject()
    bridge = PtyBridge(parent)
    program = Program(qtbot, bridge)
    program.run(CHILDREN + "time.sleep(60)")
    program.wait(" end")
    pid = int(re.search(r"pid (\d+) end", program.output).group(1))
    session = bridge._session
    sip.delete(parent)
    qtbot.waitUntil(lambda: not alive(pid), timeout=5000)
    assert session.join(2)
    assert gc.isenabled() and gui_gc._users == 0


def test_gui_keeps_running_during_a_flood(qtbot):
    bridge = PtyBridge()
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(50)
    bridge.start([sys.executable, "-c", "while True: print('x' * 70)"], Path.cwd(), {}, 24, 80)
    try:
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            bridge.read(1000)
            qtbot.wait(1)
        gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
        assert len(ticks) > 10 and max(gaps) < 0.3
    finally:
        bridge.stop()
