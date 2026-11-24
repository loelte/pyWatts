"""Training plans: planned units, their status, and matching with finished rides."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

from pywatts.core.metrics import normalized_power, training_load
from pywatts.core.workout import Workout, flatten, power_at, total_duration

KINDS = ("indoor", "outdoor", "rest", "note")
RIDE_KINDS = ("indoor", "outdoor")  # can be ridden, finished and missed
MAX_TEXT = 4000
MAX_TITLE = 120
MAX_TARGET_S = 24 * 3600
MAX_TARGET_LOAD = 1000.0
TOLERANCE = 0.3  # of duration or load, for automatic matches
DONE_FROM = 0.8  # fraction of the target; below this the unit is only partly done
_FTP = 1000  # load does not depend on it
_MAX_STEPS = 20_000


class Status(Enum):
    PLANNED = "planned"
    DONE = "done"
    PARTIAL = "partial"
    MISSED = "missed"
    SKIPPED = "skipped"


def _check_text(name: str, value: object, limit: int) -> None:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} must be text of at most {limit} characters")


def _check_number(name: str, value: object, limit: float) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= limit:
        raise ValueError(f"{name} must be above 0 and at most {limit:g}")


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    start_day: date
    end_day: date
    goal: str = ""
    notes: str = ""
    archived: bool = False
    origin: str = "user"  # user | ai

    def __post_init__(self) -> None:
        _check_text("name", self.name, MAX_TITLE)
        if not self.name.strip():
            raise ValueError("a plan needs a name")
        _check_text("goal", self.goal, MAX_TEXT)
        _check_text("notes", self.notes, MAX_TEXT)
        if self.end_day < self.start_day:
            raise ValueError("a plan cannot end before it starts")


@dataclass(frozen=True)
class PlanUnit:
    """One planned item. workout: the live library workout, else the stored snapshot."""

    id: str
    day: date  # local
    kind: str
    title: str
    notes: str = ""
    plan_id: str | None = None
    position: int = 0  # order within the day
    workout: Workout | None = None
    target_s: int | None = None
    target_load: float | None = None
    skipped: bool = False
    origin: str = "user"  # user | ai

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        _check_text("title", self.title, MAX_TITLE)
        if not self.title.strip():
            raise ValueError("a unit needs a title")
        _check_text("notes", self.notes, MAX_TEXT)
        _check_number("target_s", self.target_s, MAX_TARGET_S)
        _check_number("target_load", self.target_load, MAX_TARGET_LOAD)
        if self.workout is not None and self.kind != "indoor":
            raise ValueError("only indoor units can have a workout")

    @property
    def rideable(self) -> bool:
        return self.kind in RIDE_KINDS

    @property
    def duration_s(self) -> int | None:
        """Planned duration; the workout decides, if there is one."""
        if self.workout is not None:
            return total_duration(self.workout) or None
        return self.target_s

    @property
    def load(self) -> float | None:
        if self.workout is not None:
            return planned_load(self.workout) or None
        return self.target_load


@dataclass(frozen=True)
class Ride:
    """What planning needs to know about a finished session."""

    session_id: int
    day: date  # local day of the start
    duration_s: float | None
    load: float | None


def planned_load(workout: Workout) -> float:
    """Training load if ridden exactly. Sprints have no target and count as 0: too low then."""
    blocks = flatten(workout)
    seconds = total_duration(workout)
    if not blocks:
        return 0.0
    if seconds > _MAX_STEPS:  # bounded work; the 30 s smoothing hardly matters here
        moments = [(i + 0.5) * seconds / _MAX_STEPS for i in range(_MAX_STEPS)]
        mean4 = sum(power_at(blocks, t) ** 4 for t in moments) / _MAX_STEPS
        return training_load(mean4**0.25 * _FTP, seconds, _FTP)
    powers = [power_at(blocks, t + 0.5) * _FTP for t in range(seconds)]
    load = training_load(normalized_power(powers), seconds, _FTP)
    return load if math.isfinite(load) else 0.0


def has_sprints(workout: Workout) -> bool:
    return any(b.open for b in flatten(workout))


def compliance(unit: PlanUnit, ride: Ride) -> float | None:
    """Ridden fraction of the plan, by duration, else by load. None without a target."""
    if unit.duration_s and ride.duration_s is not None:
        return ride.duration_s / unit.duration_s
    if unit.load and ride.load is not None:
        return ride.load / unit.load
    return None


def status(unit: PlanUnit, ride: Ride | None, today: date) -> Status:
    if ride is not None and unit.rideable:
        part = compliance(unit, ride)
        return Status.DONE if part is None or part >= DONE_FROM else Status.PARTIAL
    if unit.skipped:
        return Status.SKIPPED
    if unit.rideable and unit.day < today:
        return Status.MISSED
    return Status.PLANNED


def _difference(unit: PlanUnit, ride: Ride) -> float | None:
    """Smallest relative difference within the tolerance, None if they do not fit."""
    pairs = ((unit.duration_s, ride.duration_s), (unit.load, ride.load))
    found = [abs(real - want) / want for want, real in pairs if want and real is not None]
    best = min(found, default=None)
    return best if best is not None and best <= TOLERANCE else None


def match(
    units: Iterable[PlanUnit],
    rides: Iterable[Ride],
    rejected: Iterable[tuple[str, int]] = (),
) -> list[tuple[str, int]]:
    """(unit id, session id) pairs that belong together. All inputs are still unlinked.

    Same day, similar duration or load, and each is the other's only best partner: on a tie
    nothing is linked, because a wrong link is worse than none.
    """
    refused = set(rejected)
    rides = list(rides)
    scores: dict[tuple[str, int], float] = {}
    for unit in units:
        if not unit.rideable or unit.skipped:
            continue
        for ride in rides:
            if ride.day != unit.day or (unit.id, ride.session_id) in refused:
                continue
            score = _difference(unit, ride)
            if score is not None:
                scores[unit.id, ride.session_id] = score

    def only_best(pair: tuple[str, int], index: int) -> bool:
        rivals = [s for p, s in scores.items() if p != pair and p[index] == pair[index]]
        return all(scores[pair] < s for s in rivals)

    return [p for p in scores if only_best(p, 0) and only_best(p, 1)]


def week_start(day: date) -> date:
    """Monday."""
    return day - timedelta(days=day.weekday())


def daily_loads(rides: Sequence[Ride], first: date, last: date) -> list[float]:
    """Summed load per day, first..last inclusive."""
    sums: dict[date, float] = {}
    for ride in rides:
        sums[ride.day] = sums.get(ride.day, 0.0) + (ride.load or 0.0)
    return [sums.get(first + timedelta(days=i), 0.0) for i in range((last - first).days + 1)]
