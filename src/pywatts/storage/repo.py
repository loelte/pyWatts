"""Repository over one sqlite3 connection. GUI thread only."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from pywatts.core.metrics import (
    average,
    kilojoules,
    normalized_power,
    peak_powers,
    training_load,
)
from pywatts.core.workout import Workout, from_json, to_json
from pywatts.storage import db

log = logging.getLogger(__name__)

# settings keys
FTP_W = "ftp_w"
UNITS = "units"  # "metric" | "imperial"
RIDE_TILES = "ride_tiles"  # ordered, comma separated tile ids of the ride page; "" = default
RIDE_KEYS = "ride_keys"  # "action=Key;..." overrides of the ride page shortcuts; "" = default
NAV_HIDDEN = "nav_hidden"  # "1" = the sidebar is hidden
TRAINER_ADDR = "trainer_addr"
TRAINER_NAME = "trainer_name"
HR_ADDR = "hr_addr"
HR_NAME = "hr_name"
PM_ADDR = "pm_addr"  # power meter
PM_NAME = "pm_name"
POWER_MATCH = "power_match"  # "1" = on
NAME = "name"
SEX = "sex"
BIRTH_DATE = "birth_date"
HEIGHT_CM = "height_cm"
WEIGHT_KG = "weight_kg"
RESTING_HR = "resting_hr"
MAX_HR = "max_hr"
THRESHOLD_HR = "threshold_hr"
POWER_ZONES = "power_zones"  # custom limits, "" = from FTP
HR_METHOD = "hr_method"  # "max" | "reserve" | "custom"
HR_ZONES = "hr_zones"  # custom limits
STRAVA_AUTO = "strava_auto"  # "1" = upload completed rides; tokens live in the OS keychain
SERVICES = ("wahoo", "garmin")  # rides are fetched from them; also values of sessions.source
COACH_COMMAND = "coach_command"  # own command line with {mcp_config}; "" = Claude Code
COACH_PATH = "coach_path"  # Claude Code executable; "" = search PATH
COACH_CONSENT = "coach_consent"  # "1" = the rider agreed to send data to the AI provider
COACH_SPLIT = "coach_split"  # "calendar,terminal" widths of the plan page
DEFAULTS: dict[str, str] = {FTP_W: "200", UNITS: "metric", HR_METHOD: "max"}
TRACKED = (FTP_W, WEIGHT_KG, RESTING_HR, MAX_HR)  # logged over time in body_metrics

FLUSH_INTERVAL_S = 10

_STATUSES = ("completed", "aborted", "crashed")
_SESSION_COLUMNS = (
    "id, workout_id, workout_snapshot_json, started_at_utc, ended_at_utc, ftp_w, status, "
    "avg_power, max_power, avg_hr, duration_s, kj, np, load, weight_kg, source, distance_m"
)
_OVERLAP = 0.5  # of the shorter ride; more than this is the same ride


@dataclass(frozen=True)
class Sample:
    t: float  # Runner.ride_s
    power: int | None
    cadence: float | None
    hr: int | None
    target_w: int
    speed: float | None  # km/h
    segment: int = 0  # +1 after every pause
    altitude: float | None = None  # m; only from imported rides


@dataclass(frozen=True)
class SessionRow:
    id: int
    workout_id: str
    workout: Workout  # decoded snapshot
    started_at_utc: datetime  # tz-aware UTC
    ended_at_utc: datetime | None
    ftp_w: int
    status: str  # completed | aborted | crashed
    avg_power: float | None
    max_power: int | None
    avg_hr: float | None
    duration_s: float | None
    kj: float | None
    np: float | None = None  # normalized power
    load: float | None = None  # core.metrics.training_load
    weight_kg: float | None = None  # at the time of the ride
    source: str = "pywatts"  # pywatts | wahoo | garmin | zwift | file
    distance_m: float | None = None  # summed from speed; None without speed


@dataclass(frozen=True)
class ImportedRide:
    """A ride recorded elsewhere. Samples are 1 Hz ride time, pauses excluded, target_w 0."""

    started_at_utc: datetime
    ended_at_utc: datetime
    title: str
    samples: list[Sample]
    manufacturer: int | None = None  # FIT manufacturer number


@dataclass(frozen=True)
class ImportOutcome:
    result: str  # imported | duplicate
    session_id: int | None  # the new or the existing session; None if it was deleted


@dataclass(frozen=True)
class Upload:
    session_id: int
    state: str  # queued | processing | done | failed
    upload_id: str | None = None
    activity_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Peak:
    watts: float
    started_at_utc: datetime
    weight_kg: float | None


@dataclass(frozen=True)
class Metric:
    id: int
    day: date
    metric: str  # one of TRACKED
    value: float


def service_key(service: str, what: str) -> str:
    """Setting of a service: "auto" ("0" = not at startup), "since" and "checked" (UTC), "full"
    ("1" = everything is wanted and not complete), "ai" ("1" = the AI coach sees its rides)."""
    return f"{service}_{what}"


def _utc_text(dt: datetime) -> str:
    # Fixed width, so text order is time order.
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


class Repo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def connection(self) -> sqlite3.Connection:
        """Shared with PlanRepo."""
        return self._conn

    @property
    def rider_id(self) -> str:
        return db.rider_id(self._conn)

    def list_workouts(self) -> list[Workout]:
        """Sorted by name, case-insensitive."""
        workouts = []
        for workout_id, text in self._conn.execute("SELECT id, json FROM workouts"):
            workout = self._decode_workout(workout_id, text)
            if workout is not None:
                workouts.append(workout)
        return sorted(workouts, key=lambda w: w.name.casefold())

    def get_workout(self, workout_id: UUID) -> Workout | None:
        row = self._conn.execute(
            "SELECT json FROM workouts WHERE id = ?", (str(workout_id),)
        ).fetchone()
        return None if row is None else self._decode_workout(str(workout_id), row[0])

    def save_workout(self, workout: Workout) -> None:
        """Upsert by the user: the AI may no longer change it (see plan_repo)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO workouts (id, name, json, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET name = excluded.name, json = excluded.json, "
                "updated_at = excluded.updated_at, origin = 'user', owner = ''",
                (str(workout.id), workout.name, to_json(workout), _utc_text(datetime.now(UTC))),
            )

    def delete_workout(self, workout_id: UUID) -> None:
        """Planned units keep a copy."""
        with self._conn:
            self._conn.execute(
                "UPDATE plan_units SET workout_json = "
                "(SELECT json FROM workouts WHERE id = workout_id) WHERE workout_id = ? "
                "AND workout_id IN (SELECT id FROM workouts) "
                "AND id NOT IN (SELECT unit_id FROM unit_links)",
                (str(workout_id),),
            )
            self._conn.execute("DELETE FROM workouts WHERE id = ?", (str(workout_id),))

    def start_session(self, workout: Workout, ftp_w: int, unit_id: str | None = None) -> int:
        """Insert with status 'crashed' and ended_at NULL, so a crash needs no further write.
        unit_id: the planned unit being ridden; ignored if gone or already linked."""
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO sessions "
                "(workout_id, workout_snapshot_json, started_at_utc, ftp_w, weight_kg, status) "
                "VALUES (?, ?, ?, ?, ?, 'crashed')",
                (
                    str(workout.id),
                    to_json(workout),
                    _utc_text(datetime.now(UTC)),
                    ftp_w,
                    self._current_weight(),
                ),
            )
            if unit_id is not None:
                # A new try takes the place of an unfinished one.
                self._conn.execute(
                    "DELETE FROM unit_links WHERE unit_id = ? AND session_id IN "
                    "(SELECT id FROM sessions WHERE status != 'completed')",
                    (unit_id,),
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO unit_links (unit_id, session_id, how) "
                    "SELECT id, ?, 'started' FROM plan_units WHERE id = ?",
                    (cursor.lastrowid, unit_id),
                )
                # The unit keeps the workout as it was ridden.
                self._conn.execute(
                    "UPDATE plan_units SET workout_json = ? WHERE id = ? "
                    "AND workout_id = ? AND id IN "
                    "(SELECT unit_id FROM unit_links WHERE session_id = ?)",
                    (to_json(workout), unit_id, str(workout.id), cursor.lastrowid),
                )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def add_samples(self, session_id: int, samples: list[Sample]) -> None:
        """One committed transaction."""
        with self._conn:
            self._insert_samples(session_id, samples)

    def _insert_samples(self, session_id: int, samples: list[Sample]) -> None:
        self._conn.executemany(
            "INSERT INTO samples "
            "(session_id, t, power, cadence, hr, target_w, speed, segment, altitude) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    session_id,
                    s.t,
                    s.power,
                    s.cadence,
                    s.hr,
                    s.target_w,
                    s.speed,
                    s.segment,
                    s.altitude,
                )
                for s in samples
            ],
        )

    def finish_session(self, session_id: int, status: str) -> None:
        """Compute summary from stored samples, set ended_at and status."""
        if status not in _STATUSES:
            raise ValueError(f"invalid session status: {status!r}")
        with self._conn:
            self._finalize(session_id, status, datetime.now(UTC), self.get_samples(session_id))

    def recover_crashed(self) -> int:
        """Finalize sessions with ended_at NULL as 'crashed' (summary; ended = start + last t).
        Delete those without samples. Called once at startup. Returns number kept."""
        kept = 0
        with self._conn:
            rows = self._conn.execute(
                "SELECT id, started_at_utc FROM sessions WHERE ended_at_utc IS NULL"
            ).fetchall()
            for session_id, started in rows:
                samples = self.get_samples(session_id)
                if not samples:
                    self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                    continue
                ended = _parse_utc(started) + timedelta(seconds=samples[-1].t)
                self._finalize(session_id, "crashed", ended, samples)
                kept += 1
        return kept

    def list_sessions(self) -> list[SessionRow]:
        """Newest first."""
        rows = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions ORDER BY started_at_utc DESC, id DESC"
        )
        return [self._session_row(row) for row in rows]

    def get_session(self, session_id: int) -> SessionRow | None:
        row = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return None if row is None else self._session_row(row)

    def get_samples(self, session_id: int) -> list[Sample]:
        rows = self._conn.execute(
            "SELECT t, power, cadence, hr, target_w, speed, segment, altitude FROM samples "
            "WHERE session_id = ? ORDER BY t, rowid",
            (session_id,),
        )
        return [Sample(*row) for row in rows]

    def speed_altitude(self, session_id: int) -> list[tuple[float | None, float | None]]:
        """Of every sample: much faster than get_samples for long rides."""
        return self._conn.execute(
            "SELECT speed, altitude FROM samples WHERE session_id = ? ORDER BY t, rowid",
            (session_id,),
        ).fetchall()

    def delete_session(self, session_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def get_setting(self, key: str) -> str:
        """Falls back to DEFAULTS, then ''."""
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return DEFAULTS.get(key, "") if row is None else row[0]

    def set_setting(self, key: str, value: str) -> None:
        with self._conn:
            self._put_setting(key, value)

    def _put_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @staticmethod
    def _decode_workout(workout_id: str, text: str) -> Workout | None:
        try:
            return from_json(text)
        except ValueError:
            log.warning("skipping corrupt workout %s", workout_id, exc_info=True)
            return None

    @staticmethod
    def _session_row(row: tuple) -> SessionRow:
        session_id, workout_id, snapshot, started, ended, *rest = row
        try:
            workout = from_json(snapshot)
        except ValueError:
            log.warning("session %s has a corrupt workout snapshot", session_id, exc_info=True)
            try:
                placeholder_id = UUID(workout_id)
            except ValueError:
                placeholder_id = uuid4()
            workout = Workout("(unreadable workout)", id=placeholder_id)
        return SessionRow(
            session_id,
            workout_id,
            workout,
            _parse_utc(started),
            None if ended is None else _parse_utc(ended),
            *rest,
        )

    def _finalize(
        self, session_id: int, status: str, ended: datetime, samples: list[Sample]
    ) -> None:
        powers = [s.power for s in samples if s.power is not None]
        hrs = [s.hr for s in samples if s.hr is not None]
        speeds = [s.speed for s in samples if s.speed is not None]
        self._conn.execute(
            "UPDATE sessions SET status = ?, ended_at_utc = ?, avg_power = ?, max_power = ?, "
            "avg_hr = ?, duration_s = ?, kj = ?, distance_m = ? WHERE id = ?",
            (
                status,
                _utc_text(ended),
                average(powers) if powers else None,
                max(powers, default=None),
                average(hrs) if hrs else None,
                samples[-1].t + 1.0 if samples else 0.0,  # each sample covers 1 s
                kilojoules(powers) if powers else None,
                sum(speeds) / 3.6 if speeds else None,  # km/h, 1 s per sample
                session_id,
            ),
        )
        self._store_analysis(session_id, samples)

    def _store_analysis(self, session_id: int, samples: list[Sample]) -> None:
        """Normalized power, load and peak powers. Missing power inside a ride counts as 0."""
        if not any(s.power is not None for s in samples):
            return
        segments: dict[int, list[float]] = {}
        for s in samples:
            segments.setdefault(s.segment, []).append(float(s.power or 0))
        np_w = normalized_power([s.power or 0 for s in samples])
        (ftp,) = self._conn.execute(
            "SELECT ftp_w FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        self._conn.execute(
            "UPDATE sessions SET np = ?, load = ? WHERE id = ?",
            (np_w, training_load(np_w, len(samples), ftp), session_id),
        )
        self._conn.execute("DELETE FROM session_peaks WHERE session_id = ?", (session_id,))
        self._conn.executemany(
            "INSERT INTO session_peaks (session_id, duration_s, watts) VALUES (?, ?, ?)",
            [(session_id, d, w) for d, w in peak_powers(list(segments.values())).items()],
        )

    def backfill_analysis(self) -> None:
        """Analyse rides recorded by versions without it. Called once at startup."""
        with self._conn:
            rows = self._conn.execute(
                "SELECT id FROM sessions WHERE ended_at_utc IS NOT NULL AND np IS NULL "
                "AND avg_power IS NOT NULL"
            ).fetchall()
            for (session_id,) in rows:
                self._store_analysis(session_id, self.get_samples(session_id))

    # --- imported rides ---

    def import_session(
        self, ride: ImportedRide, source: str, external_id: str, sha256: str
    ) -> ImportOutcome:
        """Store a ride recorded elsewhere, unless it is known. One transaction."""
        if not ride.samples:
            raise ValueError("ride has no samples")
        with self._conn:
            known = self._conn.execute(
                "SELECT session_id FROM imports WHERE (source = ? AND external_id = ?) "
                "OR sha256 = ? ORDER BY session_id IS NULL",
                (source, external_id, sha256),
            ).fetchone()
            if known is not None:
                outcome = ImportOutcome("duplicate", known[0])
            elif (same := self._overlapping(ride)) is not None:
                outcome = ImportOutcome("duplicate", same)
            else:
                outcome = ImportOutcome("imported", self._insert_ride(ride, source))
            self._put_import(source, external_id, sha256, outcome.session_id, outcome.result)
        return outcome

    def skip_import(self, source: str, external_id: str, sha256: str | None = None) -> None:
        """Remember an item that is not to be imported (e.g. a run)."""
        with self._conn:
            self._put_import(source, external_id, sha256, None, "skipped")

    def known_imports(self, source: str) -> set[str]:
        """External ids already handled, whatever the result."""
        rows = self._conn.execute("SELECT external_id FROM imports WHERE source = ?", (source,))
        return {external_id for (external_id,) in rows}

    def service_sessions(self, service: str) -> set[int]:
        """Rides fetched from the service, not those imported from its files."""
        rows = self._conn.execute(
            "SELECT session_id FROM imports WHERE source = ? AND result = 'imported' "
            "AND external_id NOT LIKE 'sha256:%' AND session_id IS NOT NULL",
            (service,),
        )
        return {session_id for (session_id,) in rows}

    def hidden_from_ai(self) -> set[int]:
        """Rides of services whose rides the rider does not share with the AI coach."""
        hidden: set[int] = set()
        for service in SERVICES:
            if self.get_setting(service_key(service, "ai")) != "1":
                hidden |= self.service_sessions(service)
        return hidden

    def forget_service(self, service: str) -> int:
        """Delete everything fetched from a service. Unlike after deleting a single ride, all of
        it can be fetched or imported again. Returns the number of rides."""
        with self._conn:
            rides = self.service_sessions(service)
            for session_id in rides:
                self._conn.execute("DELETE FROM imports WHERE session_id = ?", (session_id,))
                self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._conn.execute(
                "DELETE FROM imports WHERE source = ? AND external_id NOT LIKE 'sha256:%'",
                (service,),
            )
        return len(rides)

    def _put_import(
        self,
        source: str,
        external_id: str,
        sha256: str | None,
        session_id: int | None,
        result: str,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO imports VALUES (?, ?, ?, ?, ?, ?)",
            (source, external_id, sha256, session_id, result, _utc_text(datetime.now(UTC))),
        )

    def _overlapping(self, ride: ImportedRide) -> int | None:
        rows = self._conn.execute(
            "SELECT id, started_at_utc, ended_at_utc FROM sessions WHERE ended_at_utc IS NOT NULL "
            "AND started_at_utc < ? AND ended_at_utc > ?",
            (_utc_text(ride.ended_at_utc), _utc_text(ride.started_at_utc)),
        )
        for session_id, started_text, ended_text in rows:
            started, ended = _parse_utc(started_text), _parse_utc(ended_text)
            common = min(ended, ride.ended_at_utc) - max(started, ride.started_at_utc)
            shorter = min(ended - started, ride.ended_at_utc - ride.started_at_utc)
            if common > shorter * _OVERLAP:
                return session_id
        return None

    def _insert_ride(self, ride: ImportedRide, source: str) -> int:
        workout = Workout(ride.title)
        day = ride.started_at_utc.astimezone().date()  # metrics are logged by local day
        ftp = self._metric_on(FTP_W, day) or float(DEFAULTS[FTP_W])
        cursor = self._conn.execute(
            "INSERT INTO sessions (workout_id, workout_snapshot_json, started_at_utc, ftp_w, "
            "weight_kg, status, source) VALUES (?, ?, ?, ?, ?, 'completed', ?)",
            (
                str(workout.id),
                to_json(workout),
                _utc_text(ride.started_at_utc),
                max(1, round(ftp)),
                self._metric_on(WEIGHT_KG, day),
                source,
            ),
        )
        session_id = cursor.lastrowid
        assert session_id is not None
        self._insert_samples(session_id, ride.samples)
        self._finalize(session_id, "completed", ride.ended_at_utc, ride.samples)
        return session_id

    def _metric_on(self, metric: str, day: date) -> float | None:
        """Newest entry up to that day, else the oldest one, else the current setting."""
        row = self._conn.execute(
            "SELECT value FROM body_metrics WHERE metric = ? "
            "ORDER BY day > ?, CASE WHEN day <= ? THEN day END DESC, day LIMIT 1",
            (metric, day.isoformat(), day.isoformat()),
        ).fetchone()
        try:
            value = float(row[0] if row else self.get_setting(metric))
        except ValueError:
            return None
        return value if 0 < value < 10_000 else None

    # --- uploads ---

    def get_upload(self, session_id: int) -> Upload | None:
        row = self._conn.execute(
            "SELECT session_id, state, upload_id, activity_id, error FROM uploads "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return None if row is None else Upload(*row)

    def list_uploads(self, *states: str) -> list[Upload]:
        """Oldest ride first; no states = all."""
        rows = self._conn.execute(
            "SELECT session_id, state, upload_id, activity_id, error FROM uploads "
            "ORDER BY session_id"
        )
        return [u for u in (Upload(*row) for row in rows) if not states or u.state in states]

    def save_upload(self, upload: Upload) -> None:
        """Upsert."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO uploads VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (session_id) DO UPDATE "
                "SET state = excluded.state, upload_id = excluded.upload_id, "
                "activity_id = excluded.activity_id, error = excluded.error, "
                "updated_at = excluded.updated_at",
                (
                    upload.session_id,
                    upload.state,
                    upload.upload_id,
                    upload.activity_id,
                    upload.error,
                    _utc_text(datetime.now(UTC)),
                ),
            )

    # --- progress ---

    def session_peaks(self, session_id: int) -> dict[int, float]:
        return dict(
            self._conn.execute(
                "SELECT duration_s, watts FROM session_peaks WHERE session_id = ?", (session_id,)
            ).fetchall()
        )

    def best_peaks(
        self, since: datetime | None = None, without: Collection[int] = ()
    ) -> dict[int, Peak]:
        """Best per duration over finished rides, optionally only those started since.
        without: ids of sessions to leave out."""
        rows = self._conn.execute(
            "SELECT p.duration_s, p.watts, s.started_at_utc, s.weight_kg, s.id "
            "FROM session_peaks p JOIN sessions s ON s.id = p.session_id "
            "WHERE s.started_at_utc >= ? ORDER BY p.watts, s.started_at_utc DESC",
            (_utc_text(since) if since else "",),
        )
        # ascending: the highest (on ties the earliest) ends up in the dict
        return {d: Peak(w, _parse_utc(at), kg) for d, w, at, kg, i in rows if i not in without}

    def record_metric(self, metric: str, value: float, day: date) -> None:
        """Add or replace the day's entry; the newest entry becomes the current setting."""
        with self._conn:
            self._put_metric(metric, value, day)

    def _put_metric(self, metric: str, value: float, day: date) -> None:
        if metric not in TRACKED or not 0 < value < 10_000:
            raise ValueError(f"invalid {metric}: {value}")
        self._conn.execute(
            "INSERT INTO body_metrics (day, metric, value) VALUES (?, ?, ?) "
            "ON CONFLICT (day, metric) DO UPDATE SET value = excluded.value",
            (day.isoformat(), metric, value),
        )
        (newest,) = self._conn.execute(
            "SELECT value FROM body_metrics WHERE metric = ? ORDER BY day DESC LIMIT 1", (metric,)
        ).fetchone()
        self._put_setting(metric, f"{newest:g}")

    def list_metrics(self, metric: str | None = None) -> list[Metric]:
        """Newest first."""
        rows = self._conn.execute(
            "SELECT id, day, metric, value FROM body_metrics WHERE ? IS NULL OR metric = ? "
            "ORDER BY day DESC, metric",
            (metric, metric),
        )
        return [Metric(i, date.fromisoformat(d), m, v) for i, d, m, v in rows]

    def delete_metric(self, metric_id: int) -> None:
        """Remove a log entry; the current setting falls back to the newest remaining one."""
        with self._conn:
            row = self._conn.execute(
                "SELECT metric FROM body_metrics WHERE id = ?", (metric_id,)
            ).fetchone()
            if row is None:
                return
            self._conn.execute("DELETE FROM body_metrics WHERE id = ?", (metric_id,))
            newest = self._conn.execute(
                "SELECT value FROM body_metrics WHERE metric = ? ORDER BY day DESC LIMIT 1", row
            ).fetchone()
            if newest is not None:
                self._put_setting(row[0], f"{newest[0]:g}")

    def save_profile_values(
        self, values: dict[str, str], tracked: dict[str, float | None], day: date
    ) -> None:
        """One transaction. tracked: changed metrics; None clears the current value."""
        with self._conn:
            for key, value in values.items():
                self._put_setting(key, value)
            for metric, number in tracked.items():
                if number is not None:
                    self._put_metric(metric, number, day)
                else:
                    self._put_setting(metric, "")

    def _current_weight(self) -> float | None:
        try:
            return float(self.get_setting(WEIGHT_KG)) or None
        except ValueError:
            return None


class SessionRecorder:
    """Buffers samples; commits whenever >= FLUSH_INTERVAL_S of sample time is buffered.

    A failed flush keeps the buffer (retried at next flush). finish() flushes, then
    finish_session; idempotent.
    """

    def __init__(
        self, repo: Repo, workout: Workout, ftp_w: int, unit_id: str | None = None
    ) -> None:
        self.session_id: int = -1
        self._repo = repo
        self._buffer: list[Sample] = []
        self._buffered_since: float | None = None  # sample time of the last commit
        self._finished = False
        self.session_id = repo.start_session(workout, ftp_w, unit_id)

    def add(self, sample: Sample) -> None:
        # Never raises on database errors: the ride goes on and the buffer is retried.
        if self._finished:
            return
        self._buffer.append(sample)
        if self._buffered_since is None:
            self._buffered_since = sample.t
        if sample.t - self._buffered_since >= FLUSH_INTERVAL_S:
            try:
                self.flush()
            except sqlite3.Error:
                log.exception("flush failed; keeping %d samples", len(self._buffer))

    def flush(self) -> None:
        # Raises sqlite3.Error on failure, with the buffer intact.
        if not self._buffer:
            return
        self._repo.add_samples(self.session_id, self._buffer)
        self._buffered_since = self._buffer[-1].t
        self._buffer = []

    def finish(self, status: str) -> None:
        # On error nothing is marked finished, so the call can be repeated.
        if self._finished:
            return
        self.flush()
        self._repo.finish_session(self.session_id, status)
        self._finished = True
