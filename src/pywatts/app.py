"""CLI and wiring."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from functools import partial
from pathlib import Path

from PyQt6.QtCore import QEvent, QLockFile, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from pywatts import platform
from pywatts.ai.service import CoachService
from pywatts.storage.plan_repo import PlanRepo
from pywatts.storage.repo import Repo
from pywatts.storage.riders import Rider, Riders
from pywatts.sync.base import ActivitySource, KeyringStore, forget, http_client
from pywatts.sync.garmin import GarminClient
from pywatts.sync.service import SyncService
from pywatts.sync.strava import StravaClient
from pywatts.sync.thread import SyncBridge
from pywatts.sync.wahoo import WahooClient
from pywatts.trainer.base import HeartRateMonitor, PowerMeterMonitor, Trainer
from pywatts.trainer.fake import FakeHrStrap, FakePowerMeter, FakeTrainer
from pywatts.trainer.radio import Radio
from pywatts.trainer.thread import TrainerBridge
from pywatts.ui import theme
from pywatts.ui.main_window import PROFILE, MainWindow
from pywatts.ui.ride_controller import RideController
from pywatts.ui.riders import RiderButton, RiderPicker

ICON = Path(__file__).parent / "ui" / "assets" / "icon.png"

SIGNAL_POLL_MS = 500
FULL_SPEED_MS = 1000  # after the window is shown, so that it is painted too

# Bridges of earlier riders: a thread that did not end in time must not lose its object.
_ended: list[object] = []


def _ftms_factory(radio: Radio) -> Trainer:
    from pywatts.trainer.ftms import FtmsTrainer  # bleak must first load on the BLE thread

    return FtmsTrainer(radio=radio)


def _hr_factory(radio: Radio) -> HeartRateMonitor:
    from pywatts.trainer.hr import HrStrap

    return HrStrap(radio=radio)


def _pm_factory(radio: Radio) -> PowerMeterMonitor:
    from pywatts.trainer.power import PowerMeter

    return PowerMeter(radio=radio)


def make_bridge(fake: bool) -> TrainerBridge:
    if fake:
        made: list[FakeTrainer] = []

        def trainer() -> Trainer:
            made.append(FakeTrainer())
            return made[0]

        return TrainerBridge(
            trainer,
            hr_factory=FakeHrStrap,
            pm_factory=lambda: FakePowerMeter(lambda: made[0].pedalling),
        )
    radio = Radio()  # belongs to the BLE loop of this bridge
    return TrainerBridge(
        partial(_ftms_factory, radio),
        hr_factory=partial(_hr_factory, radio),
        pm_factory=partial(_pm_factory, radio),
    )


def _strava_factory(account: str) -> StravaClient:
    return StravaClient(KeyringStore(), http_client(), account=account)


def _wahoo_factory(account: str) -> ActivitySource:
    return WahooClient(KeyringStore(), http_client(), account=account)


def _garmin_factory(account: str) -> ActivitySource:
    return GarminClient(KeyringStore(), http_client(), account=account)


SOURCES = {"wahoo": _wahoo_factory, "garmin": _garmin_factory}


def sync_account(db: Path, rider_id: str) -> str:
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
    platform.set_full_speed(True)  # until run_rider has shown the window
    app = QApplication(sys.argv[:1])
    app.setApplicationName("pyWatts")
    app.setDesktopFileName(platform.APP_ID)  # icon under Wayland
    app.setWindowIcon(QIcon(str(ICON)))
    theme.apply(app)
    quit_on_signals(app)
    db = args.db or default_db(args.fake)
    locks = lock_all(db, args.fake)
    if isinstance(locks, str):
        QMessageBox.critical(None, "pyWatts", locks)
        return 1

    try:
        return run_riders(app, Riders(db), args.fake)
    finally:
        unlock_all(locks)


def quit_on_signals(app: QApplication) -> None:
    """Leave the event loop, dialogs included, so that run_rider frees the trainer."""
    if platform.on_termination(lambda: app.exit(0)):
        wake = QTimer(app)  # Python sees signals only while it runs
        wake.timeout.connect(lambda: None)
        wake.start(SIGNAL_POLL_MS)


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
    platform.set_full_speed(True)  # no window between two riders either
    repo = Repo(conn)
    bridge = make_bridge(fake)
    controller = RideController(repo, bridge)
    # Simulated rides must never reach the network.
    account = sync_account(rider.path, rider.id)
    sync_bridge = SyncBridge(
        None if fake else partial(_strava_factory, account),
        sources={} if fake else {n: partial(f, account) for n, f in SOURCES.items()},
    )
    sync = SyncService(repo, sync_bridge, controller)
    coach_config = platform.config_dir() / "coach" / rider.id
    coach = CoachService(repo, PlanRepo(conn), controller, coach_config)

    def forget_login(other: Rider) -> None:
        if not fake:
            services = ("strava", *SOURCES)
            forget(KeyringStore(), services, sync_account(other.path, other.id))

    button = RiderButton(riders, repo, rider, forget_login)
    window = MainWindow(repo, bridge, controller, sync, coach, button)
    if is_new:
        window.sidebar.setCurrentRow(PROFILE)  # FTP first
    window.show()
    QTimer.singleShot(FULL_SPEED_MS, partial(platform.set_full_speed, False))
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
