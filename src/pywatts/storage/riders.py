"""The riders of one installation: a file each, and the library they share. No Qt.

The first rider has the root file itself, the others are in a folder next to it. The list is a
table of the library; the name in it is a copy of the one in the profile.
"""

from __future__ import annotations

import builtins
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pywatts.storage import db
from pywatts.storage.repo import NAME, TRAINER_ADDR, TRAINER_NAME, UNITS, Repo

log = logging.getLogger(__name__)

MAX_NAME = 40
UNNAMED = "Rider 1"
REMOVED = "removed"  # folder
_SAME_DEVICE = (TRAINER_ADDR, TRAINER_NAME, UNITS)  # a new rider starts with these


class RiderError(ValueError):
    """The text is meant for the user."""


@dataclass(frozen=True)
class Rider:
    id: str
    name: str
    path: Path
    last_used: str  # ISO, "" = never


class Riders:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.library = db.default_library(root)
        self.folder = root.with_name(f"{root.stem}-riders")

    def list(self) -> builtins.list[Rider]:
        """By name. Riders whose file is gone are left out; files the library does not know
        (it was restored or lost, or the file was put back) are found too."""
        riders = {r.id: r for r in self._registered() if r.path.exists()}
        known = {r.path for r in riders.values()}
        for path in (self.root, *sorted(self.folder.glob("*.db"))):
            if path not in known and path.exists():
                rider = self._read(path)
                if rider is not None:
                    riders.setdefault(rider.id, rider)
        return sorted(riders.values(), key=lambda r: (r.name.casefold(), r.id))

    def _registered(self) -> builtins.list[Rider]:
        if not self.library.exists():
            return []
        with closing(sqlite3.connect(self.library, timeout=db.BUSY_TIMEOUT_S)) as conn:
            try:
                rows = conn.execute("SELECT id, name, file, last_used FROM riders").fetchall()
            except sqlite3.OperationalError as e:
                if "no such table" in str(e):  # of an older version
                    return []
                raise
        return [Rider(i, name, self._path(file), used) for i, name, file, used in rows]

    @staticmethod
    def _read(path: Path) -> Rider | None:
        try:
            uri = f"{path.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=db.BUSY_TIMEOUT_S)) as conn:
                found = dict(conn.execute("SELECT key, value FROM settings"))
        except sqlite3.Error:
            log.warning("skipping unreadable %s", path, exc_info=True)
            return None
        name = clean_name(str(found.get(NAME, ""))) or UNNAMED
        return Rider(str(found.get(db.RIDER_ID, "")), name, path, "")

    def open(self, path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        return db.open_db(path, self.library)

    def register(self, conn: sqlite3.Connection, path: Path, used: bool = True) -> Rider:
        """Add or update the rider whose file is open. used: is riding now."""
        rider = db.rider_id(conn)
        file = self._file(path)
        now = datetime.now(UTC).isoformat(timespec="microseconds") if used else ""
        with conn:
            # The file was replaced by another one, e.g. from a backup.
            conn.execute("DELETE FROM library.riders WHERE file = ? AND id != ?", (file, rider))
            conn.execute(
                "INSERT INTO library.riders (id, name, file, last_used) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET name = excluded.name, file = excluded.file, "
                "last_used = MAX(last_used, excluded.last_used)",
                (rider, clean_name(Repo(conn).get_setting(NAME)) or UNNAMED, file, now),
            )
            row = conn.execute(
                "SELECT name, last_used FROM library.riders WHERE id = ?", (rider,)
            ).fetchone()
        return Rider(rider, row[0], path, row[1])

    def add(self, name: str, device: Repo) -> Rider:
        """A new rider with an empty history. device: whose trainer and units to take over."""
        name = clean_name(name)
        if not name:
            raise RiderError("Enter a name.")
        if any(r.name.casefold() == name.casefold() for r in self.list()):
            raise RiderError(f"There already is a rider called {name}.")
        path = self.folder / f"{uuid4().hex}.db"
        try:
            with closing(self.open(path)) as conn:
                repo = Repo(conn)
                for key in _SAME_DEVICE:
                    repo.set_setting(key, device.get_setting(key))
                repo.set_setting(NAME, name)
                return self.register(conn, path, used=False)
        except BaseException:
            for suffix in ("", "-wal", "-shm"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
            raise

    def remove(self, rider: Rider) -> Path:
        """Take off the list; the file is kept. Returns where. Not for a rider in use.

        Moving the file back to `folder` brings the rider back."""
        if not self.can_remove(rider):
            raise RiderError("The first rider cannot be removed.")
        with closing(sqlite3.connect(rider.path, timeout=db.BUSY_TIMEOUT_S)) as conn:
            # Everything into the one file that is moved.
            mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
        if mode != "delete":
            raise RiderError("The data of this rider is in use.")
        name = "".join(c for c in rider.name if c.isascii() and c.isalnum())
        target = self.folder / REMOVED / f"{name or 'rider'}-{rider.path.name}"
        target.parent.mkdir(exist_ok=True)
        rider.path.rename(target)
        with closing(sqlite3.connect(self.library, timeout=db.BUSY_TIMEOUT_S)) as conn, conn:
            conn.execute("DELETE FROM riders WHERE id = ?", (rider.id,))
        return target

    def can_remove(self, rider: Rider) -> bool:
        return self._file(rider.path) != ""

    def _file(self, path: Path) -> str:
        return "" if path == self.root else path.name

    def _path(self, file: str) -> Path:
        return self.folder / Path(file).name if file else self.root


def clean_name(text: str) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())[:MAX_NAME].strip()
