"""Garbage collection on the GUI thread only.

The automatic collector runs on whichever thread allocates. On the sync worker (FIT decoding
allocates a lot) it would destroy Qt objects that are part of a reference cycle on the wrong
thread, which crashes the app.
"""

from __future__ import annotations

import gc

from PyQt6.QtCore import QObject, QTimer

INTERVAL_MS = 1000
_users = 0  # GUI thread only
_was_enabled = True


class GuiGc(QObject):
    """Active from construction until release(). GUI thread only."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        global _users, _was_enabled
        if _users == 0:
            _was_enabled = gc.isenabled()
            gc.disable()
        _users += 1
        self._released = False
        self._ticks = 0
        self._timer = QTimer(self)
        self._timer.setInterval(INTERVAL_MS)
        self._timer.timeout.connect(self.collect)
        self._timer.start()

    def collect(self) -> None:
        self._ticks += 1
        gc.collect(2 if self._ticks % 60 == 0 else 1 if self._ticks % 10 == 0 else 0)

    def release(self) -> None:
        """Call only after the worker thread has ended. Idempotent."""
        global _users
        if self._released:
            return
        self._released = True
        self._timer.stop()
        _users -= 1
        if _users == 0 and _was_enabled:
            gc.enable()
