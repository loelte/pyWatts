"""SQLite connection + schema migrations (PRAGMA user_version). GUI thread only.

One file per rider. The workouts are in a second one, attached as `library`, which riders can
share. No rider file has a table of that name, so a plain `workouts` means the library.
A transaction over both files is not atomic if the power fails during its commit (WAL)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from uuid import uuid4

# Short: the GUI thread waits on locks.
BUSY_TIMEOUT_S = 1.0

RIDER_ID = "rider_id"  # settings key
_RIDER_ID = re.compile(r"[0-9a-f]{32}")  # also a folder name and part of a keychain entry

# Entry i upgrades user_version i -> i+1. Append only; never edit released entries.
MIGRATIONS: list[tuple[str, ...]] = [
    (
        """CREATE TABLE workouts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workout_id TEXT NOT NULL,
            workout_snapshot_json TEXT NOT NULL,
            started_at_utc TEXT NOT NULL,
            ended_at_utc TEXT,
            ftp_w INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('completed', 'aborted', 'crashed')),
            avg_power REAL,
            max_power INTEGER,
            avg_hr REAL,
            duration_s REAL,
            kj REAL
        )""",
        # No uniqueness on t: a duplicate timestamp must not make a flush fail forever.
        """CREATE TABLE samples (
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            t REAL NOT NULL,
            power INTEGER,
            cadence REAL,
            hr INTEGER,
            target_w INTEGER NOT NULL,
            speed REAL
        )""",
        "CREATE INDEX samples_session_t ON samples(session_id, t)",
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ),
    (
        "ALTER TABLE samples ADD COLUMN segment INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN np REAL",
        "ALTER TABLE sessions ADD COLUMN load REAL",
        "ALTER TABLE sessions ADD COLUMN weight_kg REAL",
        """CREATE TABLE session_peaks (
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            duration_s INTEGER NOT NULL,
            watts REAL NOT NULL,
            PRIMARY KEY (session_id, duration_s)
        )""",
        """CREATE TABLE body_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            UNIQUE (day, metric)
        )""",
    ),
    (
        "ALTER TABLE sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'pywatts'",
        """CREATE TABLE uploads (
            session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            state TEXT NOT NULL CHECK (state IN ('queued', 'processing', 'done', 'failed')),
            upload_id TEXT,
            activity_id TEXT,
            error TEXT,
            updated_at TEXT NOT NULL
        )""",
        # Outlives its session, so that a deleted ride is not imported again.
        """CREATE TABLE imports (
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            sha256 TEXT,
            session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
            result TEXT NOT NULL CHECK (result IN ('imported', 'duplicate', 'skipped')),
            imported_at TEXT NOT NULL,
            PRIMARY KEY (source, external_id)
        )""",
        "CREATE INDEX imports_sha256 ON imports(sha256)",
        "CREATE INDEX imports_session ON imports(session_id)",
    ),
    ("ALTER TABLE samples ADD COLUMN altitude REAL",),
    (
        "ALTER TABLE workouts ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'",
        """CREATE TABLE plans (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            start_day TEXT NOT NULL,
            end_day TEXT NOT NULL,
            goal TEXT NOT NULL,
            notes TEXT NOT NULL,
            archived INTEGER NOT NULL,
            origin TEXT NOT NULL
        )""",
        # workout_id and plan_id are no FKs: units survive, undo restores in any order.
        """CREATE TABLE plan_units (
            id TEXT PRIMARY KEY,
            plan_id TEXT,
            day TEXT NOT NULL,
            position INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('indoor', 'outdoor', 'rest', 'note')),
            title TEXT NOT NULL,
            notes TEXT NOT NULL,
            workout_id TEXT,
            workout_json TEXT,
            target_s INTEGER,
            target_load REAL,
            skipped INTEGER NOT NULL,
            origin TEXT NOT NULL
        )""",
        "CREATE INDEX plan_units_day ON plan_units(day)",
        """CREATE TABLE unit_links (
            unit_id TEXT PRIMARY KEY REFERENCES plan_units(id) ON DELETE CASCADE,
            session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
            how TEXT NOT NULL CHECK (how IN ('started', 'auto', 'manual'))
        )""",
        """CREATE TABLE unit_rejections (
            unit_id TEXT NOT NULL REFERENCES plan_units(id) ON DELETE CASCADE,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            PRIMARY KEY (unit_id, session_id)
        )""",
        "CREATE INDEX unit_rejections_session ON unit_rejections(session_id)",
        """CREATE TABLE changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch TEXT NOT NULL,
            at_utc TEXT NOT NULL,
            actor TEXT NOT NULL,
            summary TEXT NOT NULL,
            tbl TEXT NOT NULL,
            row_id TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT,
            undone_at_utc TEXT
        )""",
        "CREATE INDEX changes_batch ON changes(batch)",
    ),
    ("DROP TABLE main.workouts",),  # see _move_workouts
]
_MOVE_STEP = 5  # the entry above

# Same rules, for the library. Names must be qualified.
LIBRARY_MIGRATIONS: list[tuple[str, ...]] = [
    (
        """CREATE TABLE library.workouts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            origin TEXT NOT NULL DEFAULT 'user'
        )""",
    ),
    # The rider whose AI made it; '' for workouts of origin 'user'.
    ("ALTER TABLE library.workouts ADD COLUMN owner TEXT NOT NULL DEFAULT ''",),
    # file: name within the riders folder, '' = the first rider. See riders.py.
    (
        """CREATE TABLE library.riders (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            file TEXT NOT NULL UNIQUE,
            last_used TEXT NOT NULL
        )""",
    ),
]


def default_library(path: Path | str) -> Path:
    path = Path(path)
    return path.with_name(f"{path.stem}-library.db")


def backup_path(path: Path | str) -> Path:
    """The rider file as it was before its workouts moved to the library."""
    path = Path(path)
    return path.with_name(f"{path.name}.before-library.bak")


def open_db(path: Path | str, library: Path | str | None = None) -> sqlite3.Connection:
    """Open/create, WAL, foreign_keys ON, migrate. Newer user_version -> RuntimeError.
    library: shared with other riders; default: a file of its own next to `path`.

    Library: workouts(id,name,json,updated_at,origin,owner), riders(id,name,file,last_used).
    Tables: sessions(id,workout_id,workout_snapshot_json,
    started_at_utc,ended_at_utc,ftp_w,status,avg_power,max_power,avg_hr,duration_s,kj,np,load,
    weight_kg,source), uploads(session_id,state,upload_id,activity_id,error,updated_at),
    imports(source,external_id,sha256,session_id,result,imported_at),
    samples(session_id,t,power,cadence,hr,target_w,speed,segment,altitude) and
    session_peaks(session_id,duration_s,watts) cascade on session delete,
    settings(key,value), body_metrics(id,day,metric,value).
    plans, plan_units, unit_links, unit_rejections, changes: see plan_repo.
    sessions.workout_id is no FK: history survives workout deletion.
    """
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)
    try:
        # Invalid UTF-8 in one row must not make whole queries fail.
        conn.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
        conn.execute("ATTACH ? AS library", (str(library or default_library(path)),))
        # Both before any change.
        for schema, migrations in (("main", MIGRATIONS), ("library", LIBRARY_MIGRATIONS)):
            version = _user_version(conn, schema)
            if version > len(migrations):
                raise RuntimeError(
                    f"{schema} database version {version} is newer than supported "
                    f"({len(migrations)})"
                )
        conn.execute("PRAGMA main.journal_mode = WAL")
        conn.execute("PRAGMA library.journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        _migrate(conn, "library", LIBRARY_MIGRATIONS)
        _migrate(conn, "main", MIGRATIONS[:_MOVE_STEP])
        if len(MIGRATIONS) > _MOVE_STEP == _user_version(conn):
            _move_workouts(conn, backup_path(path))
        _migrate(conn, "main", MIGRATIONS)
        _claim_workouts(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def rider_id(conn: sqlite3.Connection) -> str:
    """Random, made at the first use; tells riders apart who share a library."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (RIDER_ID,)).fetchone()
    if row is not None and _RIDER_ID.fullmatch(row[0]):
        return row[0]
    new = uuid4().hex
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (RIDER_ID, new),
        )
    return new


def _claim_workouts(conn: sqlite3.Connection) -> None:
    """AI workouts from before libraries could be shared: they can only be this rider's."""
    rider = rider_id(conn)
    unowned = "origin = 'ai' AND owner = ''"
    if conn.execute(f"SELECT 1 FROM library.workouts WHERE {unowned} LIMIT 1").fetchone():
        with conn:
            conn.execute(f"UPDATE library.workouts SET owner = ? WHERE {unowned}", (rider,))


def _user_version(conn: sqlite3.Connection, schema: str = "main") -> int:
    return conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0]


def _migrate(conn: sqlite3.Connection, schema: str, migrations: list[tuple[str, ...]]) -> None:
    for index, statements in enumerate(migrations):
        if _user_version(conn, schema) > index:
            continue
        with conn:  # commits, or rolls back schema and version together
            conn.execute("BEGIN IMMEDIATE")
            if _user_version(conn, schema) > index:  # another process was faster
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute(f"PRAGMA {schema}.user_version = {index + 1}")


def _move_workouts(conn: sqlite3.Connection, backup: Path) -> None:
    """Copy them to the library. Committed before the migration drops the old table, so that
    a crash in between loses nothing: the next start copies again."""
    if conn.execute("SELECT 1 FROM main.workouts LIMIT 1").fetchone() is None:
        return
    rider = rider_id(conn)
    if not backup.exists():
        part = backup.with_name(backup.name + ".part")
        part.unlink(missing_ok=True)
        conn.execute("VACUUM main INTO ?", (str(part),))
        part.replace(backup)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if _user_version(conn) != _MOVE_STEP:  # another process was faster
            return
        conn.execute(
            "INSERT INTO library.workouts (id, name, json, updated_at, origin, owner) "
            "SELECT id, name, json, updated_at, origin, "
            "CASE origin WHEN 'ai' THEN ? ELSE '' END FROM main.workouts WHERE true "
            "ON CONFLICT (id) DO UPDATE SET name = excluded.name, json = excluded.json, "
            "updated_at = excluded.updated_at, origin = excluded.origin, "
            "owner = excluded.owner WHERE excluded.updated_at > updated_at",
            (rider,),
        )
