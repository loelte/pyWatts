import sqlite3

import pytest

from pywatts.storage import db


def test_open_creates_schema_and_is_reopenable(tmp_path):
    path = tmp_path / "a.db"
    db.open_db(path).close()
    conn = db.open_db(str(path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"workouts", "sessions", "samples", "settings"} <= tables
    conn.close()


def test_newer_version_rejected(tmp_path):
    path = tmp_path / "a.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 999")
    conn.close()
    with pytest.raises(RuntimeError):
        db.open_db(path)


@pytest.mark.parametrize("error", ["NOT SQL", "CREATE TABLE settings (x)"])
def test_failed_migration_rolls_back(tmp_path, monkeypatch, error):
    path = tmp_path / "a.db"
    db.open_db(path).close()
    good = list(db.MIGRATIONS)
    monkeypatch.setattr(db, "MIGRATIONS", [*good, ("CREATE TABLE extra (x)", error)])
    with pytest.raises(sqlite3.Error):
        db.open_db(path)
    monkeypatch.setattr(db, "MIGRATIONS", good)
    conn = db.open_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(good)
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='extra'").fetchall()
    conn.close()


def test_failed_first_migration_then_success(tmp_path, monkeypatch):
    path = tmp_path / "a.db"
    with monkeypatch.context() as m:
        m.setattr(db, "MIGRATIONS", [(*db.MIGRATIONS[0], "NOT SQL")])
        with pytest.raises(sqlite3.Error):
            db.open_db(path)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
    raw.close()
    db.open_db(path).close()
