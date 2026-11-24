"""The rider: personal data and zones, read from settings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pywatts.core import zones
from pywatts.core.zones import Zones
from pywatts.storage import repo as r
from pywatts.storage.repo import Repo

HR_METHODS = ("max", "reserve", "custom")


@dataclass(frozen=True)
class Profile:
    """0 / None / '' = not set (except ftp_w, which always has a value)."""

    ftp_w: int
    name: str = ""
    sex: str = ""  # "" | "female" | "male" | "other"
    birth_date: date | None = None
    height_cm: float = 0.0
    weight_kg: float = 0.0
    resting_hr: int = 0
    max_hr: int = 0
    threshold_hr: int = 0
    custom_power: Zones | None = None
    hr_method: str = "max"
    custom_hr: Zones | None = None

    def age(self, today: date | None = None) -> int | None:
        if self.birth_date is None:
            return None
        today = today or date.today()
        had = (today.month, today.day) >= (self.birth_date.month, self.birth_date.day)
        return today.year - self.birth_date.year - (0 if had else 1)

    @property
    def power_zones(self) -> Zones:
        return self.custom_power or zones.power_zones(self.ftp_w)

    @property
    def hr_zones(self) -> Zones | None:
        """None if the needed heart rates are unknown."""
        if self.hr_method == "custom":
            return self.custom_hr
        resting = self.resting_hr if self.hr_method == "reserve" else 0
        if not self.max_hr or (self.hr_method == "reserve" and not 0 < resting < self.max_hr):
            return None
        try:
            return zones.hr_zones(self.max_hr, resting)
        except ValueError:
            return None


def estimate_max_hr(age: int) -> int:
    """Tanaka: 208 - 0.7 * age."""
    return round(208 - 0.7 * age)


def load_profile(repo: Repo) -> Profile:
    """Unreadable stored values count as not set."""

    def number(key: str) -> float:
        try:
            value = float(repo.get_setting(key) or 0)
        except ValueError:
            return 0.0
        return value if 0 < value < 10_000 else 0.0

    try:
        born = date.fromisoformat(repo.get_setting(r.BIRTH_DATE))
    except ValueError:
        born = None
    method = repo.get_setting(r.HR_METHOD)
    return Profile(
        ftp_w=int(number(r.FTP_W)) or int(r.DEFAULTS[r.FTP_W]),
        name=repo.get_setting(r.NAME),
        sex=repo.get_setting(r.SEX),
        birth_date=born,
        height_cm=number(r.HEIGHT_CM),
        weight_kg=number(r.WEIGHT_KG),
        resting_hr=int(number(r.RESTING_HR)),
        max_hr=int(number(r.MAX_HR)),
        threshold_hr=int(number(r.THRESHOLD_HR)),
        custom_power=zones.parse_limits(repo.get_setting(r.POWER_ZONES)),
        hr_method=method if method in HR_METHODS else "max",
        custom_hr=zones.parse_limits(repo.get_setting(r.HR_ZONES)),
    )


def save_profile(repo: Repo, new: Profile, today: date | None = None) -> None:
    """Changed tracked values (weight, FTP, heart rates) also enter the progress log."""
    old = load_profile(repo)
    values = {
        r.NAME: new.name,
        r.SEX: new.sex,
        r.BIRTH_DATE: new.birth_date.isoformat() if new.birth_date else "",
        r.HEIGHT_CM: f"{new.height_cm:g}",
        r.THRESHOLD_HR: str(new.threshold_hr),
        r.POWER_ZONES: zones.format_limits(new.custom_power) if new.custom_power else "",
        r.HR_METHOD: new.hr_method,
        r.HR_ZONES: zones.format_limits(new.custom_hr) if new.custom_hr else "",
    }
    tracked = {
        r.FTP_W: (old.ftp_w, new.ftp_w),
        r.WEIGHT_KG: (old.weight_kg, new.weight_kg),
        r.RESTING_HR: (old.resting_hr, new.resting_hr),
        r.MAX_HR: (old.max_hr, new.max_hr),
    }
    day = today or date.today()
    changed = {k: (float(b) if b else None) for k, (a, b) in tracked.items() if a != b}
    repo.save_profile_values(values, changed, day)
