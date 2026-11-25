"""Power meters over BLE (standard Cycling Power service). Read only: no calibration."""

from __future__ import annotations

from pywatts.trainer.base import PowerReading
from pywatts.trainer.cycling_power import (
    CP_MEASUREMENT,
    CP_SERVICE,
    CrankCadence,
    parse_power_measurement,
)
from pywatts.trainer.ftms import FTMS_SERVICE
from pywatts.trainer.sensor import Sensor


class PowerMeter(Sensor):
    """PowerMeterMonitor implementation."""

    NOUN = "power meter"
    UNNAMED = "Power meter"
    SERVICE = CP_SERVICE
    MEASUREMENT = CP_MEASUREMENT
    NO_SERVICE = "This device is not a power meter"
    HINT = "Turn the cranks to wake it."

    def _accepts(self, name: str, uuids: set[str]) -> bool:
        """Trainers provide the service too, but are not meant."""
        return CP_SERVICE in uuids and FTMS_SERVICE not in uuids

    def _reset(self) -> None:
        self._cadence = CrankCadence()

    def _reading(self, data: bytes) -> PowerReading | None:
        measured = parse_power_measurement(data)
        if measured.power_w is None:
            return None
        now = self._clock()
        cadence = self._cadence.update(measured, now)
        return PowerReading(now, max(0, measured.power_w), cadence, self._battery)
