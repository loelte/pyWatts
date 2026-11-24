"""Workout model. Power is a fraction of FTP (1.0 == FTP). Durations are whole seconds."""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4


def _check_int(name: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _check_power(name: str, value: Any) -> None:
    try:
        ok = not isinstance(value, bool) and math.isfinite(value) and value >= 0
    except (TypeError, OverflowError):
        ok = False
    if not ok:
        raise ValueError(f"{name} must be a finite number >= 0")


def _check_duration(value: Any) -> None:
    _check_int("duration_s", value)
    try:
        float(value)
    except OverflowError:
        raise ValueError("duration_s is too large") from None


def _check_cadence(value: Any) -> None:
    if value is not None and (
        not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 300
    ):
        raise ValueError("cadence must be None or an integer of 1..300 rpm")


@dataclass(frozen=True)
class Steady:
    """Validates in __post_init__: duration_s > 0, power >= 0 (ValueError)."""

    duration_s: int
    power: float
    cadence: int | None = None  # target rpm, optional

    def __post_init__(self) -> None:
        _check_duration(self.duration_s)
        _check_power("power", self.power)
        _check_cadence(self.cadence)


@dataclass(frozen=True)
class Ramp:
    """Linear from start to end. Same validation as Steady."""

    duration_s: int
    start: float
    end: float
    cadence: int | None = None

    def __post_init__(self) -> None:
        _check_duration(self.duration_s)
        _check_power("start", self.start)
        _check_power("end", self.end)
        _check_cadence(self.cadence)


@dataclass(frozen=True)
class Sprint:
    """All-out effort in open mode: no ERG target, the rider decides the power."""

    duration_s: int

    def __post_init__(self) -> None:
        _check_duration(self.duration_s)


@dataclass(frozen=True)
class Repeat:
    """count >= 1, children non-empty and never nested Repeats (ValueError)."""

    count: int
    children: list[Steady | Ramp | Sprint]

    def __post_init__(self) -> None:
        _check_int("count", self.count)
        if not isinstance(self.children, list | tuple) or not self.children:
            raise ValueError("children must be a non-empty list")
        if not all(isinstance(c, Steady | Ramp | Sprint) for c in self.children):
            raise ValueError("children must be Steady, Ramp or Sprint")
        # Own copy, so that later edits of the caller's list cannot bypass validation.
        object.__setattr__(self, "children", list(self.children))


Segment = Steady | Ramp | Sprint | Repeat


@dataclass
class Workout:
    name: str
    segments: list[Segment] = field(default_factory=list)
    description: str = ""
    id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True)
class Block:
    """Flattened piece of the timeline, [t0, t1) seconds. Open blocks have no target (power 0)."""

    t0: float
    t1: float
    p_start: float
    p_end: float
    cadence: int | None = None
    open: bool = False


def flatten(workout: Workout) -> list[Block]:
    """Expand repeats to contiguous blocks starting at 0. Empty workout -> []."""
    blocks: list[Block] = []
    t = 0
    for segment in workout.segments:
        repeat = isinstance(segment, Repeat)
        for _ in range(segment.count if repeat else 1):
            for part in segment.children if repeat else (segment,):
                cadence = None
                if isinstance(part, Sprint):
                    p_start = p_end = 0.0
                elif isinstance(part, Steady):
                    cadence = part.cadence
                    p_start = p_end = part.power
                else:
                    cadence = part.cadence
                    p_start, p_end = part.start, part.end
                blocks.append(
                    Block(
                        float(t),
                        float(t + part.duration_s),
                        p_start,
                        p_end,
                        cadence,
                        isinstance(part, Sprint),
                    )
                )
                t += part.duration_s
    return blocks


def total_duration(workout: Workout) -> int:
    return sum(
        s.count * sum(c.duration_s for c in s.children) if isinstance(s, Repeat) else s.duration_s
        for s in workout.segments
    )


def power_at(blocks: list[Block], t: float) -> float:
    """Interpolated FTP fraction at t; clamps to first/last block; 0.0 if no blocks."""
    if not blocks:
        return 0.0
    block = blocks[max(0, bisect_right(blocks, t, key=lambda b: b.t0) - 1)]
    if t <= block.t0:
        return block.p_start
    if t >= block.t1:
        return block.p_end
    return block.p_start + (block.p_end - block.p_start) * (t - block.t0) / (block.t1 - block.t0)


def _segment_to_dict(segment: Segment) -> dict[str, Any]:
    extra = {} if getattr(segment, "cadence", None) is None else {"cadence": segment.cadence}
    if isinstance(segment, Steady):
        return {
            "type": "steady",
            "duration_s": segment.duration_s,
            "power": segment.power,
        } | extra
    if isinstance(segment, Ramp):
        return {
            "type": "ramp",
            "duration_s": segment.duration_s,
            "start": segment.start,
            "end": segment.end,
        } | extra
    if isinstance(segment, Sprint):
        return {"type": "sprint", "duration_s": segment.duration_s}
    return {
        "type": "repeat",
        "count": segment.count,
        "children": [_segment_to_dict(c) for c in segment.children],
    }


def _typed(data: dict[str, Any], key: str, kind: type) -> Any:
    value = data[key]
    if not isinstance(value, kind):
        raise ValueError(f"{key} must be a {kind.__name__}")
    return value


def _segment_from_dict(data: Any, allow_repeat: bool) -> Any:
    if not isinstance(data, dict):
        raise ValueError("segment must be a JSON object")
    kind = data["type"]
    if kind == "steady":
        return Steady(data["duration_s"], data["power"], data.get("cadence"))
    if kind == "ramp":
        return Ramp(data["duration_s"], data["start"], data["end"], data.get("cadence"))
    if kind == "sprint":
        return Sprint(data["duration_s"])
    if kind == "repeat" and allow_repeat:
        children = [_segment_from_dict(c, False) for c in _typed(data, "children", list)]
        return Repeat(data["count"], children)
    raise ValueError(f"invalid segment type {kind!r}")


def to_json(workout: Workout) -> str:
    """{"version":1,"id","name","description","segments":[{"type":"steady|ramp|sprint|repeat",...}]}"""
    return json.dumps(
        {
            "version": 1,
            "id": str(workout.id),
            "name": workout.name,
            "description": workout.description,
            "segments": [_segment_to_dict(s) for s in workout.segments],
        }
    )


def from_json(text: str) -> Workout:
    """Inverse of to_json; raises ValueError on malformed input."""
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("workout must be a JSON object")
        version = data["version"]
        if type(version) is not int or version != 1:
            raise ValueError("unsupported workout version")
        return Workout(
            name=_typed(data, "name", str),
            segments=[_segment_from_dict(s, True) for s in _typed(data, "segments", list)],
            description=_typed(data, "description", str),
            id=UUID(_typed(data, "id", str)),
        )
    except KeyError as e:
        raise ValueError(f"missing field {e}") from e
    except (TypeError, RecursionError) as e:
        raise ValueError(f"malformed workout: {e}") from e
