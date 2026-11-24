"""The data tiles of the ride page and how their order is stored (setting ride_tiles)."""

from __future__ import annotations

# id -> caption. Order here is the default order; the first COLUMNS tiles are shown large.
TILES: dict[str, str] = {
    "power": "Power",
    "target": "Target",
    "cadence": "Cadence",
    "hr": "Heart rate",
    "time_left": "Time left",
    "block_left": "Block left",
    "target_rpm": "Target cadence",
    "speed": "Speed",
    "ride_time": "Ride time",
    "distance": "Distance",
    "calories": "Calories",
    "avg_power": "Avg power",
    "avg_hr": "Avg heart rate",
}
COLUMNS = 4
NONE = "none"  # stored when the user removed every tile ("" means: default)


def default_layout() -> list[str]:
    return list(TILES)


def parse_layout(text: str) -> list[str]:
    """Unknown and repeated ids are dropped, so that old or edited values never break the page."""
    if not text.strip():
        return default_layout()
    out: list[str] = []
    for part in text.split(","):
        key = part.strip()
        if key in TILES and key not in out:
            out.append(key)
    return out


def serialize_layout(ids: list[str]) -> str:
    return ",".join(ids) if ids else NONE
