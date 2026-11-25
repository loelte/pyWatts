"""Ending the process must free the trainer and save the ride (POSIX; see platform.py)."""

import os
import sqlite3
import subprocess
import sys

import pytest

from pywatts import platform

SCRIPT = """
import os, signal, sys
from PyQt6.QtCore import QTimer
from pywatts import app
from pywatts.core.workout import Steady, Workout
from pywatts.trainer.base import ConnectionState
from pywatts.trainer.fake import FakeTrainer

class Trainer(FakeTrainer):
    async def stop(self):
        print("halted", flush=True)
        await super().stop()

    async def disconnect(self):
        print("disconnected", flush=True)
        await super().disconnect()

class Controller(app.RideController):
    def __init__(self, repo, bridge):
        super().__init__(repo, bridge)
        bridge.connection_changed.connect(self.on_link)
        QTimer.singleShot(100, lambda: bridge.connect_trainer("x"))

    def on_link(self, state):
        if state is ConnectionState.CONNECTED:
            self.load(Workout(name="w", segments=[Steady(600, 0.5)]))
            self.start()
            QTimer.singleShot(2500, lambda: os.kill(os.getpid(), int(sys.argv[2])))

app.FakeTrainer = Trainer
app.RideController = Controller
sys.exit(app.main(["--fake", "--db", sys.argv[1]]))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("name", ["SIGTERM", "SIGINT", "SIGHUP"])
def test_signal_halts_and_saves(tmp_path, name):
    import signal

    db = tmp_path / "t.db"
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    number = str(int(getattr(signal, name)))
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(db), number],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    lines = done.stdout.split()
    assert "halted" in lines and lines[-1] == "disconnected"
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT status, duration_s FROM sessions").fetchall()
    assert [status for status, _ in rows] == ["aborted"] and rows[0][1] >= 1


def test_not_needed_on_windows(monkeypatch):
    monkeypatch.setattr(platform.sys, "platform", "win32")
    assert platform.on_termination(lambda: None) is False
