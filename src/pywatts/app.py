"""CLI and wiring."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PyQt6.QtCore import QLockFile
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox

from pywatts import platform
from pywatts.ai.service import CoachService
from pywatts.storage.db import open_db
from pywatts.storage.plan_repo import PlanRepo
from pywatts.storage.repo import Repo
from pywatts.sync.base import KeyringStore, http_client
from pywatts.sync.service import SyncService
from pywatts.sync.strava import StravaClient
from pywatts.sync.thread import SyncBridge
from pywatts.trainer.base import HeartRateMonitor, Trainer
from pywatts.trainer.fake import FakeHrStrap, FakeTrainer
from pywatts.trainer.thread import TrainerBridge
from pywatts.ui import theme
from pywatts.ui.main_window import MainWindow
from pywatts.ui.ride_controller import RideController

ICON = Path(__file__).parent / "ui" / "assets" / "icon.png"


def _ftms_factory() -> Trainer:
    from pywatts.trainer.ftms import FtmsTrainer  # bleak must first load on the BLE thread

    return FtmsTrainer()


def _hr_factory() -> HeartRateMonitor:
    from pywatts.trainer.hr import HrStrap

    return HrStrap()


def _strava_factory() -> StravaClient:
    return StravaClient(KeyringStore(), http_client())


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pywatts")
    parser.add_argument("--fake", action="store_true", help="use a simulated trainer")
    parser.add_argument("--db", type=Path, help="database file (default: user data dir)")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def default_db(fake: bool) -> Path:
    """Simulated rides never mix with real history."""
    path = platform.db_path()
    return path.with_name("pywatts-fake.db") if fake else path


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    platform.set_taskbar_identity()  # before the first window
    app = QApplication(sys.argv[:1])
    app.setApplicationName("pyWatts")
    app.setWindowIcon(QIcon(str(ICON)))
    theme.apply(app)
    db = args.db or default_db(args.fake)
    db.parent.mkdir(parents=True, exist_ok=True)

    lock = QLockFile(str(db) + ".lock")
    lock.setStaleLockTime(0)  # only a dead owner process makes the lock stale
    if not lock.tryLock(100):
        busy = lock.error() is QLockFile.LockError.LockFailedError
        text = f"pyWatts is already running with {db}." if busy else f"Cannot write to {db.parent}."
        QMessageBox.critical(None, "pyWatts", text)
        return 1

    conn = open_db(db)
    repo = Repo(conn)
    repo.recover_crashed()
    repo.backfill_analysis()
    bridge = TrainerBridge(
        FakeTrainer if args.fake else _ftms_factory,
        hr_factory=FakeHrStrap if args.fake else _hr_factory,
    )
    controller = RideController(repo, bridge)
    # Simulated rides must never reach the network.
    sync_bridge = SyncBridge(None if args.fake else _strava_factory)
    sync = SyncService(repo, sync_bridge, controller)
    coach = CoachService(repo, PlanRepo(conn), controller, platform.config_dir() / "coach")
    window = MainWindow(repo, bridge, controller, sync, coach)
    window.show()
    try:
        return app.exec()
    finally:
        controller.shutdown()
        if window.coach_panel is not None:
            window.coach_panel.shutdown()
        coach.stop()
        bridge.shutdown()
        sync_bridge.shutdown()
        conn.close()
        lock.unlock()


if __name__ == "__main__":
    sys.exit(main())
