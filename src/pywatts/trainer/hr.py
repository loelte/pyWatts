"""Heart rate straps over BLE (standard Heart Rate service)."""

from __future__ import annotations

from typing import Any

from pywatts.trainer.base import DeviceInfo, HeartRate
from pywatts.trainer.ftms import FTMS_SERVICE, HR_MEASUREMENT, HR_SERVICE, parse_hr_measurement
from pywatts.trainer.sensor import Sensor

WAHOO_NAMES = ("TICKR", "TRACKR")


def is_wahoo(name: str) -> bool:
    upper = name.upper().removeprefix("WAHOO").strip()
    return upper.startswith(WAHOO_NAMES)


class HrStrap(Sensor):
    """HeartRateMonitor implementation."""

    NOUN = "heart rate strap"
    UNNAMED = "Heart rate sensor"
    SERVICE = HR_SERVICE
    MEASUREMENT = HR_MEASUREMENT
    NO_SERVICE = "This device does not provide heart rate"
    HINT = "Is it worn and, if it has contacts, moistened?"

    def _accepts(self, name: str, uuids: set[str]) -> bool:
        """Wahoo straps do not always advertise the service; trainers are left out."""
        return FTMS_SERVICE not in uuids and (HR_SERVICE in uuids or is_wahoo(name))

    def _order(self, device: DeviceInfo) -> Any:
        return (not is_wahoo(device.name), -device.rssi)

    def _reading(self, data: bytes) -> HeartRate:
        return HeartRate(self._clock(), parse_hr_measurement(data), self._battery)
