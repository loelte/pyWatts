"""Pseudo terminal for the AI program: backends and threads without Qt, and the Qt bridge."""

from __future__ import annotations

import codecs
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path
from queue import Queue
from typing import Any, Protocol

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from pywatts.sync.gui_gc import GuiGc

log = logging.getLogger(__name__)

BUFFER_CHARS = 256 * 1024  # unread output; the program waits when it is full
QUEUE_CHARS = 4 * 1024 * 1024  # unwritten input; more is dropped
WRITE_CHARS = 4096
STOP_S = 2.0
_POLL_S = 0.1
_HANGUP_S = 0.3  # POSIX: time to end before SIGKILL

# They make Claude Code believe that it runs inside another Claude Code.
NESTING_VARIABLES = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_CHILD_SESSION",
)


def child_env(extra: Mapping[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() not in NESTING_VARIABLES}
    return env | {"TERM": "xterm-256color", "COLORTERM": "truecolor"} | dict(extra)


class _Pty(Protocol):
    """Created, read, killed and closed by the reader thread."""

    def read(self) -> str | None:
        """Blocks. None: the program ended, or interrupt() was called."""
        ...

    def write(self, text: str) -> None:
        """Writer thread; may block until the program ends."""
        ...

    def resize(self, rows: int, cols: int) -> None: ...

    def interrupt(self) -> None:
        """Any thread, never blocks: ends read() and write()."""
        ...

    def exit_code(self) -> int | None: ...

    def kill(self) -> None:
        """The whole process tree, also what an ended program left running."""
        ...

    def close(self) -> None: ...


class _Job:
    """Windows job object: ends all its processes with kill(), and when the app ends."""

    def __init__(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        class Limits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
            _fields_ = [
                ("times", ctypes.c_int64 * 2),
                ("flags", wintypes.DWORD),
                ("working_set", ctypes.c_size_t * 2),
                ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD * 2),
                ("io", ctypes.c_uint64 * 6),
                ("memory", ctypes.c_size_t * 4),
            ]

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        api.OpenProcess.restype = wintypes.HANDLE
        api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        api.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        api.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        api.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        self._api = api
        self._lock = threading.Lock()
        self._handle = api.CreateJobObjectW(None, None)
        limits = Limits(flags=0x2000)  # KILL_ON_JOB_CLOSE
        process = api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        try:
            done = (
                self._handle
                and process
                and api.SetInformationJobObject(
                    self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                )
                and api.AssignProcessToJobObject(self._handle, process)
            )
            if not done:
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            self.close()
            raise
        finally:
            if process:
                api.CloseHandle(process)

    def kill(self) -> None:
        with self._lock:
            if self._handle:
                self._api.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        """Ends what still runs."""
        with self._lock:
            if self._handle:
                self._api.CloseHandle(self._handle)
                self._handle = None


_unclosed: list[Any] = []


class _WindowsPty:
    def __init__(
        self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], rows: int, cols: int
    ) -> None:
        # Not PtyProcess: its reader can mix markers into the output.
        from winpty import PTY, WinptyError

        self._error = WinptyError
        pty: Any = PTY(cols, rows)
        arguments = subprocess.list2cmdline(argv[1:])
        block = "".join(f"{key}={value}\0" for key, value in env.items()) + "\0"
        try:
            pty.spawn(
                argv[0], cmdline=f" {arguments}" if arguments else None, cwd=str(cwd), env=block
            )
        except WinptyError as e:
            raise OSError(f"{argv[0]}: {e}") from e
        self._pty = pty
        self._job: _Job | None = None
        try:
            self._job = _Job(pty.pid)
        except OSError:
            log.warning("no job object for the AI program", exc_info=True)

    def read(self) -> str | None:
        while True:
            try:
                text = self._pty.read(blocking=True)
            except self._error:
                return None
            if text:
                return text
            if self._pty.iseof() or not self._pty.isalive():
                return None
            time.sleep(0.005)

    def write(self, text: str) -> None:
        try:
            self._pty.write(text)
        except self._error as e:
            raise OSError(str(e)) from e

    def resize(self, rows: int, cols: int) -> None:
        try:
            self._pty.set_size(cols, rows)
        except self._error as e:
            raise OSError(str(e)) from e

    def interrupt(self) -> None:
        if self._job is not None:
            self._job.kill()
        pty = self._pty
        try:
            if pty is not None:
                pty.cancel_io()
        except self._error:
            pass

    def exit_code(self) -> int | None:
        return self._pty.get_exitstatus() if self._ended() else None

    def _ended(self) -> bool:
        deadline = time.monotonic() + 1.0
        while alive := self._pty.isalive():
            if time.monotonic() > deadline:
                break
            time.sleep(0.005)
        return not alive

    def kill(self) -> None:
        if self._job is not None:
            self._job.kill()
        elif self._pty.isalive():
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(self._pty.pid)],
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
        if not self._ended():
            # Closing the pseudo console of a program that is still starting or ending can block
            # the creation of every thread in this process.
            log.warning("the AI program does not end")
            _unclosed.append(self._pty)
        self._pty = None  # closes the pseudo console


# Makes the new terminal the controlling one; safer than preexec_fn in a process with threads.
_POSIX_START = """
import fcntl, os, sys, termios
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
try:
    os.execvp(sys.argv[1], sys.argv[1:])
except OSError as e:
    sys.exit(f"{sys.argv[1]}: {e.strerror}")
"""


class _PosixPty:
    def __init__(
        self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], rows: int, cols: int
    ) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._interrupted = False
        self._master, slave = os.openpty()
        try:
            os.set_blocking(self._master, False)
            self.resize(rows, cols)
            self._process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", _POSIX_START, *argv],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=dict(env),
                start_new_session=True,
            )
        except BaseException:
            os.close(self._master)
            raise
        finally:
            os.close(slave)

    def _wait(self, write: bool = False) -> bool:
        import select

        fds = [self._master]
        reading, writing, _ = select.select([] if write else fds, fds if write else [], [], _POLL_S)
        return bool(reading or writing)

    def read(self) -> str | None:
        while not self._interrupted:
            # First poll, then select: what was written before the end is still read.
            ended = self._process.poll() is not None
            if not self._wait():
                if ended:  # a background process may still hold the terminal open
                    return None
                continue
            try:
                data = os.read(self._master, 65536)
            except BlockingIOError:
                continue
            except OSError:  # EIO: nobody has the terminal open anymore
                return None
            if not data:
                return None
            if text := self._decoder.decode(data):
                return text
        return None

    def write(self, text: str) -> None:
        data = text.encode("utf-8")
        while data and not self._interrupted and self._process.poll() is None:
            if self._wait(write=True):
                try:
                    data = data[os.write(self._master, data) :]
                except BlockingIOError:
                    pass

    def resize(self, rows: int, cols: int) -> None:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(self._master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def interrupt(self) -> None:
        self._interrupted = True

    def exit_code(self) -> int | None:
        try:
            return self._process.wait(1.0)
        except subprocess.TimeoutExpired:
            return None

    def _tree(self) -> set[int]:
        """The program and its descendants, also those in groups of their own."""
        found = {self._process.pid}
        try:
            listing = subprocess.run(
                ["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=2
            ).stdout
            rows = map(str.split, listing.splitlines())
            parents = {int(pid): int(parent) for pid, parent in rows}
        except (OSError, ValueError, subprocess.SubprocessError):
            return found
        size = 0
        while size != len(found):
            size = len(found)
            found |= {pid for pid, parent in parents.items() if parent in found}
        return found

    def kill(self) -> None:
        pid = self._process.pid
        running = self._process.poll() is None
        tree = self._tree() if running else set()
        self._signal(os.killpg, pid, signal.SIGHUP)
        if running:
            try:
                self._process.wait(_HANGUP_S)
            except subprocess.TimeoutExpired:
                pass
        for member in tree:
            if member != pid or self._process.poll() is None:
                self._signal(os.kill, member, signal.SIGKILL)
        self._signal(os.killpg, pid, signal.SIGKILL)
        try:
            self._process.wait(1.0)
        except subprocess.TimeoutExpired:
            log.warning("the AI program does not end")

    @staticmethod
    def _signal(send: Callable[[int, int], None], pid: int, number: int) -> None:
        try:
            send(pid, number)
        except (ProcessLookupError, PermissionError):
            pass

    def close(self) -> None:
        os.close(self._master)


def _open(argv: Sequence[str], cwd: Path, env: Mapping[str, str], rows: int, cols: int) -> _Pty:
    backend = _WindowsPty if sys.platform == "win32" else _PosixPty
    return backend(argv, cwd, env, rows, cols)


class _Session:
    """One run of a program. No Qt. Hands events to `post`, which is never called once closed."""

    def __init__(self, factory: Callable[[], _Pty], post: Callable[[str, object], object]) -> None:
        self._factory = factory
        self._post = post
        self._lock = threading.Lock()  # of _post and _pty
        self._closed = False
        self._pty: _Pty | None = None
        self._stop = threading.Event()
        self._output = threading.Condition()
        self._chunks: deque[str] = deque()
        self._size = 0
        self._announced = False
        self._input: Queue[str | tuple[int, int] | None] = Queue()
        self._queued = 0
        self._input_lock = threading.Lock()
        self._thread = threading.Thread(target=self._main, name="pty", daemon=True)
        self._thread.start()

    # --- any thread ---

    def read(self, max_chars: int) -> str:
        parts: list[str] = []
        with self._output:
            while max_chars > 0 and self._chunks:
                text = self._chunks.popleft()
                if len(text) > max_chars:
                    self._chunks.appendleft(text[max_chars:])
                    text = text[:max_chars]
                parts.append(text)
                max_chars -= len(text)
                self._size -= len(text)
            if not parts:
                self._announced = False  # the next output is announced again
            self._output.notify_all()
        return "".join(parts)

    def write(self, text: str) -> None:
        text = text.encode("utf-8", "ignore").decode()  # no lone surrogates
        with self._input_lock:
            if self._queued + len(text) > QUEUE_CHARS:
                log.warning("input dropped: the AI program does not read")
                return
            self._queued += len(text)
        for start in range(0, len(text), WRITE_CHARS):
            self._input.put(text[start : start + WRITE_CHARS])

    def resize(self, rows: int, cols: int) -> None:
        self._input.put((rows, cols))

    def close(self) -> None:
        """Ends the program; nothing is posted anymore. Does not wait."""
        with self._lock:
            self._closed = True
            self._post = lambda kind, payload: None  # breaks the cycle with the bridge
            self._stop.set()
            pty = self._pty
        with self._output:
            self._output.notify_all()
        self._input.put(None)
        if pty is not None:
            pty.interrupt()

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def post(self, kind: str, payload: object = None) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._post(kind, payload)
            except RuntimeError:  # C++ object deleted
                self._closed = True

    # --- reader thread ---

    def _main(self) -> None:
        code: int | None = None
        try:
            pty = self._factory()
        except Exception as e:
            log.warning("cannot start the AI program", exc_info=True)
            self.post("failed", str(e) or type(e).__name__)
            self.post("finished")
            return
        writer = threading.Thread(target=self._write, args=(pty,), name="pty-input", daemon=True)
        writer.start()
        try:
            with self._lock:
                self._pty = pty
            while not self._stop.is_set() and (text := pty.read()) is not None:
                self._append(text)
            if not self._stop.is_set():
                code = pty.exit_code()
        except Exception:
            log.exception("reading from the AI program failed")
        finally:
            self._end(pty, writer)
        self.post("finished", code)

    def _end(self, pty: _Pty, writer: threading.Thread) -> None:
        self._stop.set()
        with self._lock:
            self._pty = None
        for step in (pty.interrupt, pty.kill):
            try:
                step()
            except Exception:
                log.exception("ending the AI program failed")
        self._input.put(None)
        writer.join(1.0)
        try:
            pty.close()
        except Exception:
            log.exception("closing the terminal failed")

    def _append(self, text: str) -> None:
        with self._output:
            while self._size >= BUFFER_CHARS and not self._stop.is_set():
                self._output.wait()
            self._chunks.append(text)
            self._size += len(text)
            announce = not self._announced
            self._announced = True
        if announce:
            self.post("ready")

    # --- writer thread ---

    def _write(self, pty: _Pty) -> None:
        while (item := self._input.get()) is not None and not self._stop.is_set():
            try:
                if isinstance(item, tuple):
                    pty.resize(*item)
                    continue
                pty.write(item)
                with self._input_lock:
                    self._queued -= len(item)
            except Exception:
                log.debug("input lost", exc_info=True)
                return


_stuck: list[GuiGc] = []  # of threads that did not end: collecting must stay on the GUI thread


def _finish(session: _Session, gui_gc: GuiGc, timeout: float) -> None:
    if session.join(timeout):
        gui_gc.release()
    else:
        _stuck.append(gui_gc)
        log.warning("terminal thread did not end within %.1f s", timeout)


class PtyBridge(QObject):
    """One program at a time. GUI thread only; nothing blocks except stop(), bounded.

    After ready_read, call read() until it returns "": only then it is emitted again.
    """

    ready_read = pyqtSignal()
    failed = pyqtSignal(str)  # could not be started; finished(None) follows
    finished = pyqtSignal(object)  # int | None; not after stop(). read() still works.

    _posted = pyqtSignal(int, str, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._run = 0
        self._session: _Session | None = None
        self._gui_gc: GuiGc | None = None  # None: not running
        self._on_destroyed: Callable[[], None] | None = None
        self._posted.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)

    @property
    def is_running(self) -> bool:
        return self._gui_gc is not None

    def start(self, argv: list[str], cwd: Path, env: dict[str, str], rows: int, cols: int) -> None:
        """env is added to the environment of the app."""
        if not argv:
            raise ValueError("no program")
        self.stop()
        self._run += 1
        # Before the threads start, and without parent: it must outlive them.
        gui_gc = self._gui_gc = GuiGc()
        factory = partial(_open, list(argv), cwd, child_env(env), rows, cols)
        session = self._session = _Session(factory, partial(self._posted.emit, self._run))

        def destroyed() -> None:  # deleted by Qt without stop()
            session.close()
            try:
                _finish(session, gui_gc, STOP_S)
            except RuntimeError:  # Qt is already gone: the app ends
                pass

        self._on_destroyed = destroyed
        self.destroyed.connect(destroyed)

    def read(self, max_chars: int = 4096) -> str:
        return "" if self._session is None else self._session.read(max_chars)

    def write(self, text: str) -> None:
        if self._session is not None and self.is_running:
            self._session.write(text)

    def resize(self, rows: int, cols: int) -> None:
        if self._session is not None and self.is_running:
            self._session.resize(rows, cols)

    def stop(self, timeout: float = STOP_S) -> None:
        """Ends the program with everything it started. Idempotent."""
        if self._session is not None:
            self._session.close()
            self._release(timeout)
            self._session = None

    def _release(self, timeout: float) -> None:
        if self._on_destroyed is not None:
            self.destroyed.disconnect(self._on_destroyed)
            self._on_destroyed = None
        if self._gui_gc is not None and self._session is not None:
            _finish(self._session, self._gui_gc, timeout)
            self._gui_gc = None

    @pyqtSlot(int, str, object)
    def _dispatch(self, run: int, kind: str, payload: object) -> None:
        if run != self._run or self._session is None:
            return  # of a stopped program
        if kind == "ready":
            self.ready_read.emit()
        elif kind == "failed":
            self.failed.emit(str(payload))
        elif kind == "finished":
            self._release(STOP_S)  # posted by the last action of the thread
            self.finished.emit(payload)
