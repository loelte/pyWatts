"""What the calendar shows: entries per day, their state and colors. No widgets."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

from pywatts.core.plan import PlanUnit, Ride, Status, has_sprints, planned_load, status, week_start
from pywatts.core.workout import Workout
from pywatts.ui import theme
from pywatts.ui.ride import NO_VALUE

VIEWS = ("year", "month", "week")
# Not from strftime: Qt sets the system locale on some platforms.
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
FIRST_DAY, LAST_DAY = date(2000, 1, 1), date(2100, 12, 31)
FULL_LOAD = 100.0  # daily load with the full color in the year view
_CACHE_SIZE = 2000


@dataclass(frozen=True)
class Look:
    fill: str | None  # None: outline only
    outline: str | None
    symbol: str  # repeats the status, so that color is not the only cue
    label: str

    @property
    def text(self) -> str:
        return theme.TEXT if self.fill is None else text_on(self.fill)


LOOKS = {
    "indoor": Look(theme.PLAN_COLORS["indoor"], None, "", "Indoor"),
    "outdoor": Look(theme.PLAN_COLORS["outdoor"], None, "", "Outdoor"),
    "done": Look(theme.PLAN_COLORS["done"], None, "✓", "Done"),
    "partial": Look(None, theme.PLAN_COLORS["done"], "½", "Partly done"),
    "missed": Look(theme.PLAN_COLORS["missed"], None, "✗", "Missed"),
    "skipped": Look(theme.PLAN_COLORS["neutral"], None, "–", "Skipped"),
    "rest": Look(theme.PLAN_COLORS["neutral"], None, "", "Rest"),
    "note": Look(theme.PLAN_COLORS["neutral"], None, "", "Note"),
    "ride": Look(None, theme.PLAN_COLORS["neutral"], "+", "Unplanned"),
}
_BY_STATUS = {
    Status.DONE: "done",
    Status.PARTIAL: "partial",
    Status.MISSED: "missed",
    Status.SKIPPED: "skipped",
}
# The state a day has in the year view, if it has several entries.
_RANK = ("missed", "indoor", "outdoor", "partial", "done", "ride", "skipped", "rest", "note")


def _luminance(color: str) -> float:
    def linear(part: str) -> float:
        c = int(part, 16) / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(color[i : i + 2]) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two #rrggbb colors."""
    low, high = sorted((_luminance(a), _luminance(b)))
    return (high + 0.05) / (low + 0.05)


def text_on(fill: str) -> str:
    """The dark or white text color that reads better."""
    return max((theme.ON_ZONE, "#ffffff"), key=lambda c: contrast(c, fill))


def blend(color: str, base: str, part: float) -> str:
    def channel(i: int) -> int:
        return round(int(color[i : i + 2], 16) * part + int(base[i : i + 2], 16) * (1 - part))

    return "#" + "".join(f"{channel(i):02x}" for i in (1, 3, 5))


def hours(seconds: float | None) -> str:
    """E.g. 45 min or 1:30 h."""
    if seconds is None:
        return NO_VALUE
    minutes = round(seconds / 60)
    return f"{minutes // 60}:{minutes % 60:02d} h" if minutes >= 60 else f"{minutes} min"


def number(value: float | None) -> str:
    return NO_VALUE if value is None else f"{value:.0f}"


def day_text(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1][:3]} {day.year}"


def one_line(text: str) -> str:
    return " ".join(text.split())


class Loads:
    """Planned load per workout content; computing it takes some ms."""

    def __init__(self) -> None:
        self._known: dict[str, float] = {}

    def of_workout(self, workout: Workout) -> float | None:
        key = repr(workout.segments)
        if key not in self._known:
            if len(self._known) >= _CACHE_SIZE:
                self._known.clear()
            self._known[key] = planned_load(workout)
        return self._known[key] or None

    def of(self, unit: PlanUnit) -> float | None:
        return unit.target_load if unit.workout is None else self.of_workout(unit.workout)


def sprints(unit: PlanUnit) -> bool:
    return unit.workout is not None and has_sprints(unit.workout)


@dataclass(frozen=True)
class Entry:
    """A planned unit with its ride, or a ride without a unit."""

    day: date
    title: str
    unit: PlanUnit | None = None
    ride: Ride | None = None
    status: Status | None = None
    planned_s: int | None = None
    planned_load: float | None = None
    linked: bool = False  # also while the ride is still being recorded

    @property
    def key(self) -> tuple[str, str]:
        if self.unit is not None:
            return "unit", self.unit.id
        return "ride", "" if self.ride is None else str(self.ride.session_id)

    @property
    def state(self) -> str:
        """Key of LOOKS; the status wins over the kind."""
        if self.unit is None or self.status is None:
            return "ride"
        return _BY_STATUS.get(self.status, self.unit.kind)

    @property
    def look(self) -> Look:
        return LOOKS[self.state]

    @property
    def rideable(self) -> bool:
        return self.unit is None or self.unit.rideable

    @property
    def duration_s(self) -> float | None:
        return self.planned_s if self.ride is None else self.ride.duration_s

    @property
    def load(self) -> float | None:
        return self.planned_load if self.ride is None else self.ride.load

    @property
    def label(self) -> str:
        return f"{self.look.symbol} {self.look.label}".strip()

    def describe(self) -> str:
        """Plain text, for tooltips and screen readers."""
        lines = [self.title, self.label]
        if self.unit is not None and self.unit.rideable:
            if self.state not in ("indoor", "outdoor"):
                lines[1] += f" ({self.unit.kind})"
            estimate = " without sprints" if sprints(self.unit) else ""
            lines.append(
                f"Planned {hours(self.planned_s)}, load {number(self.planned_load)}{estimate}"
            )
        if self.ride is not None:
            lines.append(f"Ridden {hours(self.ride.duration_s)}, load {number(self.ride.load)}")
        return "\n".join(lines)


def make_entry(unit: PlanUnit, ride: Ride | None, linked: bool, today: date, loads: Loads) -> Entry:
    return Entry(
        unit.day,
        one_line(unit.title),
        unit,
        ride,
        status(unit, ride, today),
        unit.duration_s if unit.rideable else None,
        loads.of(unit) if unit.rideable else None,
        linked,
    )


@dataclass(frozen=True)
class Totals:
    planned_s: float = 0.0
    planned_load: float = 0.0
    done_s: float = 0.0
    done_load: float = 0.0
    planned: int = 0  # units
    done: int = 0  # rides

    def text(self) -> str:
        return (
            f"Planned {hours(self.planned_s)}, load {self.planned_load:.0f}   ·   "
            f"Done {hours(self.done_s)}, load {self.done_load:.0f}"
        )


@dataclass
class CalendarData:
    first: date
    last: date
    today: date
    days: dict[date, list[Entry]] = field(default_factory=dict)
    rides: list[Ride] = field(default_factory=list)  # all that count as done

    def entries(self, day: date) -> list[Entry]:
        return self.days.get(day, [])

    def find(self, key: tuple[str, str]) -> Entry | None:
        return next((e for day in self.days.values() for e in day if e.key == key), None)

    def totals(self, first: date, last: date) -> Totals:
        """Skipped units are not planned any more."""
        units = [
            e
            for day, entries in self.days.items()
            if first <= day <= last
            for e in entries
            if e.unit is not None and e.unit.rideable and e.status is not Status.SKIPPED
        ]
        rides = [r for r in self.rides if first <= r.day <= last]
        return Totals(
            sum(e.planned_s or 0 for e in units),
            sum(e.planned_load or 0 for e in units),
            sum(r.duration_s or 0 for r in rides),
            sum(r.load or 0 for r in rides),
            len(units),
            len(rides),
        )


def build(
    first: date,
    last: date,
    today: date,
    units: Iterable[PlanUnit],
    links: Mapping[str, int],
    rides: Mapping[int, Ride],
    loads: Loads,
    title_of: Callable[[Ride], str],
    filtered: bool = False,
) -> CalendarData:
    """links: unit id -> session id, of all units. rides: at least all of first..last.
    filtered: rides of units that are not in `units` do not count."""
    data = CalendarData(first, last, today)
    shown = set()
    for unit in units:
        session_id = links.get(unit.id)
        shown.add(unit.id)
        ride = None if session_id is None else rides.get(session_id)
        entry = make_entry(unit, ride, session_id is not None, today, loads)
        data.days.setdefault(unit.day, []).append(entry)
    owners = {session_id: unit_id for unit_id, session_id in links.items()}
    for ride in rides.values():
        if not first <= ride.day <= last:
            continue
        owner = owners.get(ride.session_id)
        if owner is None:
            title = one_line(title_of(ride)) or "Ride"
            data.days.setdefault(ride.day, []).append(Entry(ride.day, title, ride=ride))
        if owner is None or owner in shown or not filtered:
            data.rides.append(ride)
    return data


def main_entry(entries: list[Entry]) -> Entry | None:
    return min(entries, key=lambda e: _RANK.index(e.state), default=None)


def year_fill(entries: list[Entry]) -> str | None:
    """Status color of the day, shaded by its load. None: outline or nothing."""
    entry = main_entry(entries)
    if entry is None or entry.look.fill is None:
        return None
    if not entry.rideable:
        return entry.look.fill
    known = [e.load for e in entries if e.load is not None]
    part = min(1.0, sum(known) / FULL_LOAD) if known else 0.5
    return blend(entry.look.fill, theme.SURFACE, 0.45 + 0.55 * part)


# --- periods ---


def clamp(day: date) -> date:
    return min(max(day, FIRST_DAY), LAST_DAY)


def month_end(day: date) -> date:
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def period(view: str, day: date) -> tuple[date, date]:
    """First and last day of the week, month or year."""
    if view == "week":
        return week_start(day), week_start(day) + timedelta(days=6)
    if view == "month":
        return day.replace(day=1), month_end(day)
    return date(day.year, 1, 1), date(day.year, 12, 31)


def visible_range(view: str, day: date) -> tuple[date, date]:
    """The period, extended to whole weeks in the month view."""
    first, last = period(view, day)
    if view == "month":
        return week_start(first), week_start(last) + timedelta(days=6)
    return first, last


def shift(view: str, day: date, step: int) -> date:
    """Same day some periods later; the end of a shorter month if there is none."""
    if view == "week":
        return clamp(day + timedelta(days=7 * step))
    months = day.year * 12 + day.month - 1 + step * (12 if view == "year" else 1)
    if not FIRST_DAY.year * 12 <= months < (LAST_DAY.year + 1) * 12:
        return FIRST_DAY if step < 0 else LAST_DAY
    target = date(months // 12, months % 12 + 1, 1)
    return target.replace(day=min(day.day, month_end(target).day))


def period_label(view: str, day: date) -> str:
    first, last = period(view, day)
    if view == "year":
        return str(day.year)
    if view == "month":
        return f"{MONTHS[day.month - 1]} {day.year}"
    start = str(first.day)
    if first.month != last.month:
        start += f" {MONTHS[first.month - 1][:3]}"
    if first.year != last.year:
        start += f" {first.year}"
    return f"{start} – {last.day} {MONTHS[last.month - 1][:3]} {last.year}"
