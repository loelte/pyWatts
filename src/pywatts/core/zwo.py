"""Zwift .zwo import/export (SteadyState, Warmup, Cooldown, Ramp, IntervalsT).

FreeRide and MaxEffort (both without ERG) become Sprint; Sprint is written as FreeRide.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.parsers import expat

from pywatts.core.workout import Ramp, Repeat, Segment, Sprint, Steady, Workout

_RAMP_TAGS = ("Warmup", "Cooldown", "Ramp")
_OPEN_TAGS = ("FreeRide", "MaxEffort")
_ILLEGAL_XML = re.compile(r"[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")


def _reject_doctype(*_: object) -> None:
    raise ValueError("DTD/entity declarations are not allowed")


def _parse_xml(data: str | bytes) -> ET.Element:
    try:
        guard = expat.ParserCreate()
        guard.StartDoctypeDeclHandler = _reject_doctype
        guard.Parse(data, True)
        return ET.fromstring(data)
    except (expat.ExpatError, ET.ParseError, LookupError) as e:
        raise ValueError(f"invalid XML: {e}") from e


def _number(el: ET.Element, *groups: str) -> float:
    """Mean of the first `|`-separated attribute group that is completely present."""
    for group in groups:
        raw = [el.get(name) for name in group.split("|")]
        if None not in raw:
            return sum(float(v) for v in raw) / len(raw)
    raise ValueError(f"{el.tag}: missing attribute {groups[0]}")


def _int(el: ET.Element, name: str) -> int:
    value = _number(el, name)
    try:
        return round(value)
    except OverflowError:
        raise ValueError(f"{el.tag}: invalid {name}") from None


def _cadence(el: ET.Element, name: str = "Cadence") -> int | None:
    """Optional target rpm; a malformed or out-of-range value is ignored."""
    try:
        value = round(float(el.get(name, "")))
    except (ValueError, OverflowError):
        return None
    return value if 1 <= value <= 300 else None


def _segment(el: ET.Element) -> Segment | None:
    if el.tag == "SteadyState":
        return Steady(
            _int(el, "Duration"), _number(el, "Power", "PowerLow|PowerHigh"), _cadence(el)
        )
    if el.tag in _RAMP_TAGS:
        # Despite the names, PowerLow is the start and PowerHigh the end, also for Cooldown.
        return Ramp(
            _int(el, "Duration"), _number(el, "PowerLow"), _number(el, "PowerHigh"), _cadence(el)
        )
    if el.tag in _OPEN_TAGS:
        return Sprint(_int(el, "Duration"))
    if el.tag == "IntervalsT":
        on = _number(el, "OnPower", "PowerOnLow|PowerOnHigh")
        off = _number(el, "OffPower", "PowerOffLow|PowerOffHigh")
        children = [
            Steady(_int(el, "OnDuration"), on, _cadence(el, "Cadence")),
            Steady(_int(el, "OffDuration"), off, _cadence(el, "CadenceResting")),
        ]
        return Repeat(_int(el, "Repeat"), children)
    return None


def _from_xml(data: str | bytes) -> Workout:
    root = _parse_xml(data)
    body = root.find("workout")
    if body is None:
        raise ValueError("no <workout> element")
    return Workout(
        name=(root.findtext("name") or "").strip(),
        segments=[s for s in map(_segment, body) if s is not None],
        description=(root.findtext("description") or "").strip(),
    )


def _add(body: ET.Element, tag: str, **attrs: float) -> None:
    ET.SubElement(body, tag, {k: repr(v) for k, v in attrs.items()})


def loads(text: str) -> Workout:
    """Parse; new id. Unknown elements are skipped; invalid XML/no <workout> -> ValueError."""
    return _from_xml(text.lstrip("﻿ \t\r\n"))


def dumps(workout: Workout) -> str:
    """Repeat of exactly two Steady -> IntervalsT; other repeats are expanded."""
    root = ET.Element("workout_file")
    ET.SubElement(root, "author")
    ET.SubElement(root, "name").text = _ILLEGAL_XML.sub("", workout.name)
    ET.SubElement(root, "description").text = _ILLEGAL_XML.sub("", workout.description)
    ET.SubElement(root, "sportType").text = "bike"
    body = ET.SubElement(root, "workout")
    last = len(workout.segments) - 1
    for i, seg in enumerate(workout.segments):
        if isinstance(seg, Repeat):
            if len(seg.children) == 2 and all(isinstance(c, Steady) for c in seg.children):
                on, off = seg.children
                extra = {}
                if on.cadence is not None:
                    extra["Cadence"] = on.cadence
                if off.cadence is not None:
                    extra["CadenceResting"] = off.cadence
                _add(
                    body,
                    "IntervalsT",
                    Repeat=seg.count,
                    OnDuration=on.duration_s,
                    OffDuration=off.duration_s,
                    OnPower=on.power,
                    OffPower=off.power,
                    **extra,
                )
                continue
            parts = seg.children * seg.count
        else:
            parts = [seg]
        for part in parts:
            if isinstance(part, Sprint):
                _add(body, "FreeRide", Duration=part.duration_s, FlatRoad=1)
                continue
            extra = {} if part.cadence is None else {"Cadence": part.cadence}
            if isinstance(part, Steady):
                _add(body, "SteadyState", Duration=part.duration_s, Power=part.power, **extra)
                continue
            tag = "Ramp"
            if part is seg and i == 0 and part.end >= part.start:
                tag = "Warmup"
            elif part is seg and i == last and part.end < part.start:
                tag = "Cooldown"
            _add(
                body,
                tag,
                Duration=part.duration_s,
                PowerLow=part.start,
                PowerHigh=part.end,
                **extra,
            )
    ET.indent(root)
    return ET.tostring(root, encoding="unicode") + "\n"


def load(path: Path) -> Workout:
    return _from_xml(path.read_bytes())


def save(workout: Workout, path: Path) -> None:
    path.write_text(dumps(workout), encoding="utf-8", newline="\n")
