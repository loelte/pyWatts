"""Workout editing logic. Plain Python, no Qt.

Segments are addressed by path: (i,) is top level, (i, j) is child j of the repeat at i.
All user-caused failures raise EditError with a displayable message and leave the model unchanged.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import replace
from uuid import UUID, uuid4

from pywatts.core.workout import (
    Block,
    Ramp,
    Repeat,
    Segment,
    Sprint,
    Steady,
    Workout,
    flatten,
    total_duration,
)

Path = tuple[int, ...]

MAX_DURATION_S = 24 * 3600
MAX_PERCENT = 500
MAX_COUNT = 100
MAX_CADENCE = 300
NEW_NAME = "New workout"


class EditError(ValueError):
    """Rejected edit; str() is shown to the user."""


def parse_duration(text: str) -> int:
    """'mm:ss', 'h:mm:ss' or plain minutes."""
    text = text.strip()
    if not re.fullmatch(r"[0-9]{1,4}(:[0-9]{1,2}){0,2}", text):
        raise EditError("Duration must be mm:ss, e.g. 5:00")
    parts = [int(p) for p in text.split(":")]
    if len(parts) == 1:
        parts.append(0)
    if parts[-1] > 59 or (len(parts) == 3 and parts[1] > 59):
        raise EditError("Duration: minutes/seconds after a colon must be below 60")
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    if not 0 < seconds <= MAX_DURATION_S:
        raise EditError("Duration must be between 0:01 and 24 hours")
    return seconds


def format_duration(seconds: float) -> str:
    s = round(seconds)
    return f"{s // 60}:{s % 60:02d}"


def format_total(seconds: float) -> str:
    s = round(seconds)
    if s < 3600:
        return format_duration(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_percent(text: str) -> float:
    """'88', '88.5', '88,5 %' -> FTP fraction."""
    cleaned = text.strip().removesuffix("%").strip().replace(",", ".")
    if not re.fullmatch(r"[0-9]+(\.[0-9]*)?", cleaned):
        raise EditError("Power must be a number in % of FTP, e.g. 88")
    value = float(cleaned)
    if not math.isfinite(value) or value > MAX_PERCENT:
        raise EditError(f"Power must be between 0 and {MAX_PERCENT} % of FTP")
    return round(value / 100, 6)


def format_percent(power: float) -> str:
    return f"{power * 100:.2f}".rstrip("0").rstrip(".")


def parse_count(text: str) -> int:
    text = text.strip()
    if not re.fullmatch(r"[0-9]{1,4}", text) or not 1 <= int(text) <= MAX_COUNT:
        raise EditError(f"Repeat count must be between 1 and {MAX_COUNT}")
    return int(text)


def parse_cadence(text: str) -> int | None:
    """Empty = no target."""
    text = text.strip()
    if not text:
        return None
    if not re.fullmatch(r"[0-9]{1,3}", text) or not 1 <= int(text) <= MAX_CADENCE:
        raise EditError(f"Cadence must be empty or between 1 and {MAX_CADENCE} rpm")
    return int(text)


def watts(power: float, ftp_w: int) -> int:
    return round(power * ftp_w)


FIELD_PARSERS = {
    "duration": parse_duration,
    "power": parse_percent,
    "start": parse_percent,
    "end": parse_percent,
    "cadence": parse_cadence,
    "count": parse_count,
}


def fields_of(seg: Segment) -> tuple[str, ...]:
    if isinstance(seg, Steady):
        return ("duration", "power", "cadence")
    if isinstance(seg, Ramp):
        return ("duration", "start", "end", "cadence")
    if isinstance(seg, Sprint):
        return ("duration",)
    return ("count",)


def field_text(seg: Segment, name: str) -> str:
    if name == "duration":
        return format_duration(seg.duration_s)
    if name == "count":
        return str(seg.count)
    if name == "cadence":
        return "" if seg.cadence is None else str(seg.cadence)
    return format_percent(getattr(seg, name))


def segment_duration(seg: Segment) -> int:
    if isinstance(seg, Repeat):
        return seg.count * sum(c.duration_s for c in seg.children)
    return seg.duration_s


def default_steady() -> Steady:
    return Steady(300, 0.75)


def default_ramp() -> Ramp:
    return Ramp(300, 0.5, 0.75)


def default_sprint() -> Sprint:
    return Sprint(15)


def default_repeat() -> Repeat:
    return Repeat(4, [Steady(60, 1.0), Steady(60, 0.5)])


class EditorModel:
    def __init__(self, workout: Workout | None = None) -> None:
        self.load(workout if workout is not None else Workout(NEW_NAME))

    # --- document ---------------------------------------------------------------------------

    def load(self, workout: Workout) -> None:
        self._doc = deepcopy(workout)
        self._saved = deepcopy(workout)

    def new(self) -> None:
        self.load(Workout(NEW_NAME))

    def revert(self) -> None:
        self._doc = deepcopy(self._saved)

    def mark_saved(self) -> None:
        self._saved = deepcopy(self._doc)

    @property
    def dirty(self) -> bool:
        return self._content(self._doc) != self._content(self._saved)

    @staticmethod
    def _content(w: Workout) -> tuple:
        return (w.name, w.description, list(w.segments))

    @property
    def id(self) -> UUID:
        return self._doc.id

    @property
    def name(self) -> str:
        return self._doc.name

    @name.setter
    def name(self, value: str) -> None:
        self._doc.name = value

    @property
    def description(self) -> str:
        return self._doc.description

    @description.setter
    def description(self, value: str) -> None:
        self._doc.description = value

    @property
    def segments(self) -> tuple[Segment, ...]:
        return tuple(self._doc.segments)

    @property
    def empty(self) -> bool:
        return not self._doc.segments

    def workout(self) -> Workout:
        """Independent copy of the edited state."""
        return deepcopy(self._doc)

    def prepare_save(self) -> Workout:
        """Normalize + validate; returns the copy to store."""
        name = self._doc.name.strip()
        if not name:
            raise EditError("The workout needs a name")
        self._doc.name = name
        return self.workout()

    def blocks(self) -> list[Block]:
        return flatten(self._doc)

    def total_duration(self) -> int:
        return total_duration(self._doc)

    @staticmethod
    def copy_of(workout: Workout) -> Workout:
        dup = deepcopy(workout)
        dup.id = uuid4()
        dup.name = f"{workout.name} (copy)"
        return dup

    # --- segments ---------------------------------------------------------------------------

    def get(self, path: Path) -> Segment:
        try:
            if len(path) not in (1, 2) or any(i < 0 for i in path):
                raise IndexError
            seg: Segment = self._doc.segments[path[0]]
            if len(path) == 2:
                if not isinstance(seg, Repeat):
                    raise IndexError
                seg = seg.children[path[1]]
        except IndexError:
            raise EditError("No segment selected") from None
        return seg

    def add(self, seg: Segment, after: Path | None = None) -> Path:
        """Insert after a path (a repeat always goes to the top level), or append."""
        if after is None:
            path: Path = (len(self._doc.segments),)
        else:
            self.get(after)
            if isinstance(seg, Repeat):
                after = after[:1]
            path = (*after[:-1], after[-1] + 1)
        self._insert(path, seg)
        return path

    def add_child(self, path: Path, seg: Steady | Ramp | Sprint) -> Path:
        """Append to the repeat at path, or to the repeat that contains path."""
        rep = self.get(path[:1])
        if not isinstance(rep, Repeat):
            raise EditError("Select a repeat first")
        if isinstance(seg, Repeat):
            raise EditError("Repeats cannot be nested")
        new = (path[0], len(rep.children))
        self._insert(new, seg)
        return new

    def duplicate(self, path: Path) -> Path:
        seg = deepcopy(self.get(path))
        new = (*path[:-1], path[-1] + 1)
        self._insert(new, seg)
        return new

    def delete(self, path: Path) -> Path | None:
        """Returns the path to select next."""
        self.get(path)
        items = self._siblings(path)
        if len(path) == 2 and len(items) == 1:
            raise EditError("A repeat needs at least one segment; delete the repeat instead")
        del items[path[-1]]
        self._store(path, items)
        if not items:
            return None
        return (*path[:-1], min(path[-1], len(items) - 1))

    def move(self, path: Path, delta: int) -> Path:
        """Swap within its siblings; no-op at the edges."""
        self.get(path)
        items = self._siblings(path)
        i, j = path[-1], path[-1] + delta
        if not 0 <= j < len(items):
            return path
        items[i], items[j] = items[j], items[i]
        self._store(path, items)
        return (*path[:-1], j)

    def set_field(self, path: Path, name: str, text: str) -> None:
        """Parse text for one of fields_of(segment) and apply it."""
        seg = self.get(path)
        if name not in fields_of(seg):
            raise EditError(f"This segment has no {name}")
        value = FIELD_PARSERS[name](text)
        attr = "duration_s" if name == "duration" else name
        try:
            new = replace(seg, **{attr: value})
        except ValueError as e:
            raise EditError(str(e)) from e
        items = self._siblings(path)
        items[path[-1]] = new
        self._store(path, items)

    def _siblings(self, path: Path) -> list:
        if len(path) == 1:
            return list(self._doc.segments)
        return list(self.get(path[:1]).children)

    def _store(self, path: Path, items: list) -> None:
        if len(path) == 1:
            self._doc.segments = items
        else:
            self._doc.segments[path[0]] = replace(self.get(path[:1]), children=items)

    def _insert(self, path: Path, seg: Segment) -> None:
        items = self._siblings(path)
        items.insert(path[-1], seg)
        self._store(path, items)
