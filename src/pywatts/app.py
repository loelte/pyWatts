"""CLI and wiring."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from functools import partial
from pathlib import Path

from PyQt6.QtCore import QEvent, QLockFile
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from pywatts import platform
from pywatts.ai.service import CoachService
from pywatts.storage.plan_repo import PlanRepo
from pywatts.storage.repo import Repo
from pywatts.storage.riders import Rider, Riders
from pywatts.sync.base import KeyringStore, http_client
from pywatts.sync.service import SyncService
from pywatts.sync.strava import StravaClient, forget
from pywatts.sync.thread import SyncBridge
from pywatts.trainer.base import HeartRateMonitor, Trainer
from pywatts.trainer.fake import FakeHrStrap, FakeTrainer
from pywatts.trainer.thread import TrainerBridge
from pywatts.ui import theme
from pywatts.ui.main_window import PROFILE, MainWindow
from pywatts.ui.ride_controller import RideController
from pywatts.ui.riders import RiderButton, RiderPicker

ICON = Path(__file__).parent / "ui" / "assets" / "icon.png"

# Bridges of earlier riders: a thread that did not end in time must not lose its object.
_ended: list[object] = []


def _ftms_factory() -> Trainer:
    from pywatts.trainer.ftms import FtmsTrainer  # bleak must first load on the BLE thread

    return FtmsTrainer()


def _hr_factory() -> HeartRateMonitor:
    from pywatts.trainer.hr import HrStrap

    return HrStrap()


def _strava_factory(account: str) -> StravaClient:
    return StravaClient(KeyringStore(), http_client(), account=account)


def strava_account(db: Path, rider_id: str) -> str:
    """Every database has a login of its own: rides must never reach another rider's account.
    The default one has the login from before there were several."""
    return "" if db.resolve() == platform.db_path().resolve() else rider_id


def lock(path: Path, what: str) -> QLockFile | str:
    """The held lock, or why there is none."""
    path.parent.mkdir(parents=True, exist_ok=True)
    held = QLockFile(str(path))
    held.setStaleLockTime(0)  # only a dead owner process makes the lock stale
    if held.tryLock(100):
        return held
    if held.error() is QLockFile.LockError.LockFailedError:
        return f"{what}."
    return f"Cannot write to {path.parent}."


def lock_all(db: Path, fake: bool) -> list[QLockFile] | str:
    """The database, and the trainer: riders must not compete for it."""
    wanted = [(db.with_name(db.name + ".lock"), f"pyWatts is already running with {db}")]
    if not fake:
        wanted.insert(0, (platform.lock_path(), "pyWatts is already running"))
    locks: list[QLockFile] = []
    for path, what in wanted:
        held = lock(path, what)
        if isinstance(held, str):
            unlock_all(locks)
            return held
        locks.append(held)
    return locks


def unlock_all(locks: list[QLockFile]) -> None:
    for held in locks:
        held.unlock()


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
    locks = lock_all(db, args.fake)
    if isinstance(locks, str):
        QMessageBox.critical(None, "pyWatts", locks)
        return 1

    try:
        return run_riders(app, Riders(db), args.fake)
    finally:
        unlock_all(locks)


def known(riders: Riders) -> list[Rider]:
    """[] if the library is unusable: opening the first rider then tells why."""
    try:
        return riders.list()
    except sqlite3.Error:
        return []


def choose(riders: Riders) -> Path | None:
    """The file of the rider; asks if there are several. None: cancelled."""
    found = known(riders)
    if len(found) < 2:
        return found[0].path if found else riders.root
    picker = RiderPicker(found)
    rider = picker.chosen() if picker.exec() == QDialog.DialogCode.Accepted else None
    return None if rider is None else rider.path


def run_riders(app: QApplication, riders: Riders, fake: bool) -> int:
    """One rider after the other, until a window is closed without choosing the next."""
    path, is_new = choose(riders), False
    while path is not None:
        try:
            conn = riders.open(path)
            try:
                rider = riders.register(conn, path)
                repo = Repo(conn)
                repo.recover_crashed()
                repo.backfill_analysis()
            except BaseException:
                conn.close()
                raise
        except (sqlite3.Error, RuntimeError, OSError) as e:
            logging.getLogger(__name__).exception("opening %s failed", path)
            QMessageBox.critical(None, "pyWatts", f"Cannot open {path}: {e}")
            if len(known(riders)) < 2:
                return 1
            path, is_new = choose(riders), False
            continue
        following, is_new, free = run_rider(app, riders, rider, conn, fake, is_new)
        if following is None:
            break
        if not free:  # two of them must not control one trainer
            text = "The trainer is still connected. Please start pyWatts again."
            QMessageBox.critical(None, "pyWatts", text)
            return 1
        path = following.path
    return 0


def run_rider(
    app: QApplication,
    riders: Riders,
    rider: Rider,
    conn: sqlite3.Connection,
    fake: bool,
    is_new: bool,
) -> tuple[Rider | None, bool, bool]:
    """Until the window is closed. Returns the next rider, whether they are new, and whether
    the trainer is free again. Closes conn."""
    repo = Repo(conn)
    bridge = TrainerBridge(
        FakeTrainer if fake else _ftms_factory,
        hr_factory=FakeHrStrap if fake else _hr_factory,
    )
    controller = RideController(repo, bridge)
    # Simulated rides must never reach the network.
    account = strava_account(rider.path, rider.id)
    sync_bridge = SyncBridge(None if fake else partial(_strava_factory, account))
    sync = SyncService(repo, sync_bridge, controller)
    coach_config = platform.config_dir() / "coach" / rider.id
    coach = CoachService(repo, PlanRepo(conn), controller, coach_config)

    def forget_login(other: Rider) -> None:
        if not fake:
            forget(KeyringStore(), strava_account(other.path, other.id))

    button = RiderButton(riders, repo, rider, forget_login)
    window = MainWindow(repo, bridge, controller, sync, coach, button)
    if is_new:
        window.sidebar.setCurrentRow(PROFILE)  # FTP first
    window.show()
    free = True
    try:
        app.exec()
    finally:
        controller.shutdown()
        if window.coach_panel is not None:
            window.coach_panel.shutdown()
        coach.stop()
        free = bridge.shutdown()
        sync_bridge.shutdown()
        conn.close()
        # Late signals must not reach what is deleted here.
        for source in (bridge, sync_bridge, controller, sync, coach):
            source.blockSignals(True)
        _ended.extend((bridge, sync_bridge))
        following, next_is_new = window.next_rider, window.next_is_new
        for done in (window, sync, coach, controller):
            done.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    return following, next_is_new, free


if __name__ == "__main__":
    sys.exit(main())
