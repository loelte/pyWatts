"""Keyboard shortcuts of the ride page and how they are stored (setting ride_keys)."""

from __future__ import annotations

# action id -> (caption, default key)
ACTIONS: dict[str, tuple[str, str]] = {
    "power_up": ("Power up", "Up"),
    "power_down": ("Power down", "Down"),
    "skip": ("Skip block", "Right"),
    "start": ("Start / pause / resume", "Space"),
    "extend": ("Extend block", ""),
    "stop": ("Stop ride", ""),
}


def default_keys() -> dict[str, str]:
    return {action: key for action, (_caption, key) in ACTIONS.items()}


def parse_keys(text: str) -> dict[str, str]:
    """Stored as 'action=Key;action=Key'. Missing actions keep their default; an empty key
    means unassigned. Unknown actions are dropped, so edited values never break the page."""
    keys = default_keys()
    for part in text.split(";"):
        action, sep, key = part.partition("=")
        if sep and action.strip() in ACTIONS:
            keys[action.strip()] = key.strip()
    return keys


def serialize_keys(keys: dict[str, str]) -> str:
    """ "" (the default) when nothing differs, so later default changes still apply."""
    changed = {a: k for a, k in keys.items() if a in ACTIONS and k != ACTIONS[a][1]}
    return ";".join(f"{a}={k}" for a, k in changed.items())
