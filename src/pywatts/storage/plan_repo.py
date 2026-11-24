"""Training plans, planned units, their rides, and the undo log. GUI thread only."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pywatts.core.plan import Plan, PlanUnit, Ride, match
from pywatts.core.workout import Workout, from_json, to_json

log = logging.getLogger(__name__)

ACTORS = ("user", "ai")
KEEP_BATCHES = 500

_COLUMNS = {
    "plans": ("id", "name", "start_day", "end_day", "goal", "notes", "archived", "origin"),
    "plan_units": (
        "id",
        "plan_id",
        "day",
        "position",
        "kind",
        "title",
        "notes",
        "workout_id",
        "workout_json",
        "target_s",
        "target_load",
        "skipped",
        "origin",
    ),
    "workouts": ("id", "name", "json", "origin"),
}
_LINK = "_link"  # extra entry of a deleted unit: [session id, how]


class PlanError(ValueError):
    """The text is meant for the user or the AI."""


@dataclass(frozen=True)
class Link:
    unit_id: str
    session_id: int
    how: str  # started | auto | manual


@dataclass(frozen=True)
class Change:
    batch: str
    at_utc: datetime
    actor: str  # user | ai
    summary: str
    undone: bool


@dataclass(frozen=True)
class ChangedRow:
    table: str  # plans | plan_units | workouts
    row_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None


Item = Plan | PlanUnit | Workout


def new_id() -> str:
    return str(uuid4())


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _table(item: Item) -> str:
    if isinstance(item, Plan):
        return "plans"
    return "plan_units" if isinstance(item, PlanUnit) else "workouts"


class PlanRepo:
    """Shares the connection of Repo. All changes of one apply() can be undone together."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # --- reading ---

    def list_plans(self, archived: bool = False) -> list[Plan]:
        """By start; archived plans only on request."""
        rows = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS['plans'])} FROM plans "
            "WHERE archived = 0 OR ? ORDER BY start_day, name",
            (archived,),
        )
        return [p for p in map(self._plan, rows) if p is not None]

    def get_plan(self, plan_id: str) -> Plan | None:
        row = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS['plans'])} FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
        return None if row is None else self._plan(row)

    def units_between(self, first: date, last: date, plan_id: str | None = None) -> list[PlanUnit]:
        """By day and position. Units of archived plans only if they were ridden."""
        return self._units(
            "u.day >= ? AND u.day <= ? AND (? IS NULL OR u.plan_id = ?) "
            "AND (l.session_id IS NOT NULL OR NOT EXISTS "
            "(SELECT 1 FROM plans p WHERE p.id = u.plan_id AND p.archived))",
            (first.isoformat(), last.isoformat(), plan_id, plan_id),
        )

    def get_unit(self, unit_id: str) -> PlanUnit | None:
        return next(iter(self._units("u.id = ?", (unit_id,))), None)

    def links(self) -> dict[str, Link]:
        """By unit id."""
        rows = self._conn.execute("SELECT unit_id, session_id, how FROM unit_links")
        return {row[0]: Link(*row) for row in rows}

    def rides_between(self, first: date, last: date) -> list[Ride]:
        """Finished rides by local day of their start, oldest first."""
        rows = self._conn.execute(
            "SELECT id, started_at_utc, duration_s, load FROM sessions "
            "WHERE ended_at_utc IS NOT NULL AND started_at_utc >= ? AND started_at_utc < ? "
            "ORDER BY started_at_utc",
            # a day more on both sides: the local day can differ from the UTC day
            ((first - timedelta(days=1)).isoformat(), (last + timedelta(days=3)).isoformat()),
        )
        rides = [
            Ride(i, datetime.fromisoformat(at).astimezone().date(), seconds, load)
            for i, at, seconds, load in rows
        ]
        return [r for r in rides if first <= r.day <= last]

    def workout_origin(self, workout_id: UUID) -> str | None:
        row = self._conn.execute(
            "SELECT origin FROM workouts WHERE id = ?", (str(workout_id),)
        ).fetchone()
        return None if row is None else row[0]

    def changes(self, limit: int = 50, actor: str | None = None) -> list[Change]:
        """Newest first, one per apply()."""
        rows = self._conn.execute(
            "SELECT batch, at_utc, actor, summary, undone_at_utc FROM changes "
            "WHERE ? IS NULL OR actor = ? GROUP BY batch ORDER BY MAX(id) DESC LIMIT ?",
            (actor, actor, limit),
        )
        return [
            Change(batch, datetime.fromisoformat(at), who, text, undone is not None)
            for batch, at, who, text, undone in rows
        ]

    def change_rows(self, batch: str) -> list[ChangedRow]:
        """What one apply() changed."""
        rows = self._conn.execute(
            "SELECT tbl, row_id, before_json, after_json FROM changes WHERE batch = ? ORDER BY id",
            (batch,),
        )
        return [
            ChangedRow(table, row_id, json.loads(a or "null"), json.loads(b or "null"))
            for table, row_id, a, b in rows
        ]

    # --- changing ---

    def apply(
        self,
        actor: str,
        summary: str,
        saves: Sequence[Item] = (),
        deletes: Sequence[Item] = (),
    ) -> str:
        """Save and delete in one transaction; returns the batch for undo().

        The AI must not touch workouts of the user: PlanError, nothing is changed."""
        if actor not in ACTORS:
            raise ValueError(f"invalid actor: {actor!r}")
        batch = new_id()
        with self._conn:
            for item in saves:
                self._change(batch, actor, summary, item, self._row(item, actor))
            for item in deletes:
                self._change(batch, actor, summary, item, None)
            self._trim()
        return batch

    def undo(self, batch: str, actor: str | None = None) -> None:
        """Restore what apply() changed. PlanError if it was changed again since.
        actor: only undo if this actor made the change."""
        with self._conn:
            rows = self._conn.execute(
                "SELECT actor, tbl, row_id, before_json, after_json, undone_at_utc FROM changes "
                "WHERE batch = ? ORDER BY id DESC",
                (batch,),
            ).fetchall()
            if not rows or (actor is not None and rows[0][0] != actor):
                raise PlanError("There is no such change.")
            if rows[0][5] is not None:
                raise PlanError("This change was already undone.")
            for _, table, row_id, before_text, after_text, _ in rows:
                before = None if before_text is None else json.loads(before_text)
                after = None if after_text is None else json.loads(after_text)
                if self._stored(table, row_id) != after:
                    raise PlanError("This was changed again since, so it cannot be undone.")
                self._put(table, row_id, before)
            self._conn.execute(
                "UPDATE changes SET undone_at_utc = ? WHERE batch = ?", (_now(), batch)
            )

    def link(self, unit_id: str, session_id: int, how: str = "manual") -> None:
        """Replaces existing links of both."""
        with self._conn:
            self._freeze_workout(unit_id)
            self._conn.execute(
                "DELETE FROM unit_links WHERE unit_id = ? OR session_id = ?", (unit_id, session_id)
            )
            self._conn.execute(
                "DELETE FROM unit_rejections WHERE unit_id = ? AND session_id = ?",
                (unit_id, session_id),
            )
            try:
                self._conn.execute(
                    "INSERT INTO unit_links VALUES (?, ?, ?)", (unit_id, session_id, how)
                )
            except sqlite3.IntegrityError as e:
                raise PlanError("The unit or the ride no longer exists.") from e

    def unlink(self, unit_id: str) -> None:
        """Remembered: the pair is never matched automatically again."""
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO unit_rejections "
                "SELECT unit_id, session_id FROM unit_links WHERE unit_id = ?",
                (unit_id,),
            )
            self._conn.execute("DELETE FROM unit_links WHERE unit_id = ?", (unit_id,))

    def auto_match(self, first: date, last: date) -> int:
        """Link unlinked rides and units that clearly belong together. Returns the number."""
        links = self.links().values()
        linked_units = {link.unit_id for link in links}
        linked_rides = {link.session_id for link in links}
        rejected = self._conn.execute("SELECT unit_id, session_id FROM unit_rejections").fetchall()
        pairs = match(
            [u for u in self.units_between(first, last) if u.id not in linked_units],
            [r for r in self.rides_between(first, last) if r.session_id not in linked_rides],
            rejected,
        )
        with self._conn:
            for unit_id, _ in pairs:
                self._freeze_workout(unit_id)
            self._conn.executemany("INSERT OR IGNORE INTO unit_links VALUES (?, ?, 'auto')", pairs)
        return len(pairs)

    # --- internal ---

    def _freeze_workout(self, unit_id: str) -> None:
        """Before a ride is linked: later edits of the workout no longer count."""
        self._conn.execute(
            "UPDATE plan_units SET workout_json = "
            "(SELECT json FROM workouts WHERE id = workout_id) WHERE id = ? "
            "AND workout_id IN (SELECT id FROM workouts) "
            "AND id NOT IN (SELECT unit_id FROM unit_links)",
            (unit_id,),
        )

    def _units(self, where: str, args: Iterable[Any]) -> list[PlanUnit]:
        columns = ", ".join(f"u.{c}" for c in _COLUMNS["plan_units"])
        rows = self._conn.execute(
            f"SELECT {columns}, w.json, l.session_id FROM plan_units u "
            "LEFT JOIN workouts w ON w.id = u.workout_id "
            f"LEFT JOIN unit_links l ON l.unit_id = u.id WHERE {where} "
            "ORDER BY u.day, u.position, u.title, u.id",
            tuple(args),
        )
        return [u for u in map(self._unit, rows) if u is not None]

    @staticmethod
    def _unit(row: tuple) -> PlanUnit | None:
        *columns, live, session_id = row
        data = dict(zip(_COLUMNS["plan_units"], columns, strict=True))
        snapshot = data.pop("workout_json")
        data.pop("workout_id")
        # A ridden unit keeps the workout it was planned with.
        texts = (snapshot, live) if session_id is not None else (live, snapshot)
        workout = None
        try:
            for text in filter(None, texts):
                try:
                    workout = from_json(text)
                    break
                except ValueError:
                    log.warning("planned unit %s has a corrupt workout", data["id"])
            data["day"] = date.fromisoformat(data["day"])
            data["skipped"] = bool(data["skipped"])
            return PlanUnit(**data, workout=workout if data["kind"] == "indoor" else None)
        except (ValueError, TypeError):
            log.warning("skipping corrupt planned unit %s", data["id"], exc_info=True)
            return None

    @staticmethod
    def _plan(row: tuple) -> Plan | None:
        data = dict(zip(_COLUMNS["plans"], row, strict=True))
        try:
            data["start_day"] = date.fromisoformat(data["start_day"])
            data["end_day"] = date.fromisoformat(data["end_day"])
            return Plan(**data | {"archived": bool(data["archived"])})
        except (ValueError, TypeError):
            log.warning("skipping corrupt plan %s", data["id"], exc_info=True)
            return None

    @staticmethod
    def _row(item: Item, actor: str) -> dict[str, Any]:
        if isinstance(item, Plan):
            values: tuple = (
                item.id,
                item.name,
                item.start_day.isoformat(),
                item.end_day.isoformat(),
                item.goal,
                item.notes,
                int(item.archived),
                item.origin,
            )
        elif isinstance(item, PlanUnit):
            workout = item.workout
            values = (
                item.id,
                item.plan_id,
                item.day.isoformat(),
                item.position,
                item.kind,
                item.title,
                item.notes,
                None if workout is None else str(workout.id),
                None if workout is None else to_json(workout),
                item.target_s,
                item.target_load,
                int(item.skipped),
                item.origin,
            )
        else:
            values = (str(item.id), item.name, to_json(item), actor)
        return dict(zip(_COLUMNS[_table(item)], values, strict=True))

    def _stored(self, table: str, row_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS[table])} FROM {table} WHERE id = ?", (row_id,)
        ).fetchone()
        return None if row is None else dict(zip(_COLUMNS[table], row, strict=True))

    def _change(
        self, batch: str, actor: str, summary: str, item: Item, after: dict[str, Any] | None
    ) -> None:
        table, row_id = _table(item), str(item.id)
        before = self._stored(table, row_id)
        if before == after:
            return
        if table == "workouts" and actor == "ai" and before and before["origin"] != "ai":
            raise PlanError(
                f"The workout “{before['name']}” belongs to the rider: copy it instead."
            )
        if table == "plan_units" and before is not None and after is None:
            link = self._conn.execute(
                "SELECT session_id, how FROM unit_links WHERE unit_id = ?", (row_id,)
            ).fetchone()
            if link is not None:
                before = before | {_LINK: list(link)}
        self._put(table, row_id, after)
        self._conn.execute(
            "INSERT INTO changes (batch, at_utc, actor, summary, tbl, row_id, before_json, "
            "after_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch,
                _now(),
                actor,
                summary,
                table,
                row_id,
                None if before is None else json.dumps(before),
                None if after is None else json.dumps(after),
            ),
        )

    def _put(self, table: str, row_id: str, row: dict[str, Any] | None) -> None:
        """Make the stored row equal to `row`; None deletes it."""
        if row is None:
            self._conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
            return
        row = dict(row)
        link = row.pop(_LINK, None)
        if table == "workouts":
            row["updated_at"] = _now()
        # Never REPLACE: that would delete the row first and cascade to its link.
        updates = ", ".join(f"{c} = excluded.{c}" for c in row if c != "id")
        self._conn.execute(
            f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}",
            tuple(row.values()),
        )
        if link is not None:
            self._conn.execute(
                "INSERT OR IGNORE INTO unit_links SELECT ?, id, ? FROM sessions WHERE id = ?",
                (row_id, link[1], link[0]),
            )

    def _trim(self) -> None:
        self._conn.execute(
            "DELETE FROM changes WHERE batch NOT IN "
            "(SELECT batch FROM changes GROUP BY batch ORDER BY MAX(id) DESC LIMIT ?)",
            (KEEP_BATCHES,),
        )
