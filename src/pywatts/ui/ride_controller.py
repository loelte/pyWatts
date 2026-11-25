"""Joins Runner, TrainerBridge and SessionRecorder for one ride at a time."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal

from pywatts.core.power_match import PowerMatch
from pywatts.core.runner import Runner, RunnerState
from pywatts.core.workout import Block, Workout, flatten
from pywatts.storage.repo import FLUSH_INTERVAL_S, FTP_W, POWER_MATCH, Repo, Sample, SessionRecorder
from pywatts.trainer.base import ConnectionState, HeartRate, PowerReading, Telemetry
from pywatts.trainer.thread import TrainerBridge

log = logging.getLogger(__name__)

TELEMETRY_MAX_AGE_S = 5.0
MAX_TICK_GAP_S = 10.0  # longer: the computer slept or the app hung


class RideController(QObject):
    updated = pyqtSignal()
    state_changed = pyqtSignal(object)  # RunnerState
    session_finished = pyqtSignal(int)
    error = pyqtSignal(str)

    def __init__(
        self,
        repo: Repo,
        bridge: TrainerBridge,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._bridge = bridge
        self._clock = clock
        self.workout: Workout | None = None
        self._unit_id: str | None = None  # planned unit the workout belongs to
        self.runner: Runner | None = None  # None until the loaded workout is started
        self.ftp_w = 0  # FTP of the current/last ride
        # Live trace: workout position (Runner.elapsed_s) and power (NaN = no data / jump).
        self.history_t: list[float] = []
        self.history_power: list[float] = []
        self.distance_m = 0.0  # summed from speed, per recorded second
        self.work_j = 0.0
        self._power_sum = 0.0  # over recorded seconds that had a reading
        self._power_n = 0
        self._hr_sum = 0.0
        self._hr_n = 0
        self._telemetry: Telemetry | None = None
        self._strap: HeartRate | None = None
        self._meter: PowerReading | None = None
        self._match: PowerMatch | None = None  # of the current/last ride
        self._segment = 0  # +1 on every resume
        self._next_s = 0  # next second of ride_s to record
        self._last_target = 0
        self._recorder: SessionRecorder | None = None
        self._pending: list[Sample] = []  # samples waiting for a recorder to exist
        self._unfinished: list[tuple[SessionRecorder, str]] = []  # finish() failed, retry
        # Rides whose session could not be created: workout, FTP, samples, status.
        self._unsaved: list[tuple[Workout, int, list[Sample], str, str | None]] = []
        self._storage_failed = False
        self._skipped_s = 0.0  # gap time hidden from the runner
        self._last_advance = 0.0

        bridge.telemetry.connect(self._on_telemetry)
        bridge.hr_reading.connect(self._on_heart_rate)
        bridge.hr_connection_changed.connect(self._on_hr_connection)
        bridge.pm_reading.connect(self._on_power)
        bridge.pm_connection_changed.connect(self._on_pm_connection)
        bridge.connection_changed.connect(self._on_connection)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.tick)
        self._timer.start()

    # --- state ---
    @property
    def state(self) -> RunnerState:
        return self.runner.state if self.runner else RunnerState.IDLE

    @property
    def is_active(self) -> bool:
        return self.state in (RunnerState.RUNNING, RunnerState.PAUSED)

    @property
    def blocks(self) -> list[Block]:
        """Blocks of the current ride (including extensions), else of the loaded workout."""
        if self.runner:
            return self.runner.blocks
        return flatten(self.workout) if self.workout else []

    @property
    def latest_telemetry(self) -> Telemetry | None:
        return self._telemetry_at(self._clock())

    def _telemetry_at(self, moment: float) -> Telemetry | None:
        """The power meter wins over the trainer's own power and cadence."""
        tel, meter = self._trainer_at(moment), self._meter_at(moment)
        if meter is None:
            return tel
        base = tel or Telemetry(meter.t, None, None, None)
        cadence = base.cadence_rpm if meter.cadence_rpm is None else meter.cadence_rpm
        return replace(base, power_w=meter.watts, cadence_rpm=cadence)

    def _trainer_at(self, moment: float) -> Telemetry | None:
        tel = self._telemetry
        return tel if tel and abs(moment - tel.t) <= TELEMETRY_MAX_AGE_S else None

    def _meter_at(self, moment: float) -> PowerReading | None:
        meter = self._meter
        return meter if meter and abs(moment - meter.t) <= TELEMETRY_MAX_AGE_S else None

    @property
    def latest_hr(self) -> int | None:
        return self._hr_at(self._clock())

    def _hr_at(self, moment: float) -> int | None:
        """The strap wins over the trainer's own heart rate."""
        strap = self._strap
        if strap and strap.bpm and abs(moment - strap.t) <= TELEMETRY_MAX_AGE_S:
            return strap.bpm
        tel = self._telemetry_at(moment)
        return tel.hr_bpm if tel else None

    @property
    def match_offset_w(self) -> int | None:
        """Correction of the trainer's target; None = not matching."""
        match = self._match
        if match is None or not match.enabled or not self.is_active or self._meter is None:
            return None
        return match.offset_w

    def reload_settings(self) -> None:
        """Takes effect during a ride too."""
        if self._match is not None:
            try:
                self._match.enabled = self._repo.get_setting(POWER_MATCH) == "1"
            except Exception:  # matching is optional: never in the way of a ride
                log.exception("reading the power match setting failed")

    def _connected(self) -> bool:
        return self._bridge.state is ConnectionState.CONNECTED

    # --- commands ---
    def load(self, workout: Workout, unit_id: str | None = None) -> bool:
        """unit_id: the planned unit this ride fulfils."""
        if self.is_active:
            self.error.emit("A ride is in progress. Stop it before loading another workout.")
            return False
        self.workout = deepcopy(workout)
        self._unit_id = unit_id
        self.runner = None
        self._clear_history()
        self.state_changed.emit(RunnerState.IDLE)
        self.updated.emit()
        return True

    def start(self) -> None:
        if self.is_active:
            return
        if not self._connected():
            self.error.emit("Connect a trainer first.")
            return
        if self.workout is None or not flatten(self.workout):
            self.error.emit("Choose a workout with at least one interval.")
            return
        try:
            ftp = int(float(self._repo.get_setting(FTP_W)))
            if ftp <= 0:
                raise ValueError
        except (ValueError, OverflowError):
            self.error.emit("Set a valid FTP in the settings.")
            return
        except Exception as exc:
            log.exception("reading FTP failed")
            self.error.emit(f"Could not read the FTP setting: {exc}")
            return

        self._retry_unfinished()
        self._clear_history()
        self._next_s = 0
        self._segment = 0
        self._last_target = 0
        self._recorder = None
        self._pending = []
        self._storage_failed = False
        self.ftp_w = ftp
        self._match = PowerMatch(self._bridge, self._clock)
        self.reload_settings()
        runner = Runner(self.workout, ftp, self._match, self._ride_clock)
        runner.state_changed.append(self._on_state)
        runner.finished.append(self._on_finished)
        runner.target_changed.append(lambda _w: self.updated.emit())
        runner.block_changed.append(lambda _i: self.updated.emit())
        self.runner = runner
        self._timer.start()
        self._last_advance = self._clock()
        runner.start()
        self._ensure_recorder()
        self._sample()
        self.updated.emit()

    def pause(self) -> None:
        if self.runner is None or self.runner.state is not RunnerState.RUNNING:
            return
        self._advance()
        self.runner.pause()
        if self.is_active:  # else the workout just ended
            self._ensure_recorder()
            self._flush()
        self.updated.emit()

    def resume(self) -> None:
        if self.runner is None or self.runner.state is not RunnerState.PAUSED:
            return
        if not self._connected():
            self.error.emit("Trainer not connected. Reconnect before resuming.")
            return
        self._last_advance = self._clock()
        self._segment += 1
        self.runner.resume()
        self.updated.emit()

    def skip(self) -> None:
        if not self.is_active:
            return
        assert self.runner is not None
        self._advance()
        self.runner.skip_block()
        if self.is_active:  # break the trace at the jump
            self.history_t.append(self.runner.elapsed_s)
            self.history_power.append(math.nan)
        self.updated.emit()

    def extend(self, seconds: int = 30) -> None:
        if not self.is_active:
            return
        assert self.runner is not None
        self._advance()
        self.runner.extend(seconds)
        self.updated.emit()

    def adjust_power(self, delta_w: int) -> None:
        if not self.is_active:
            return
        assert self.runner is not None
        self._advance()
        self.runner.adjust_power(delta_w)
        self._last_target = self.runner.target_w
        self.updated.emit()

    def stop(self) -> None:
        if not self.is_active:
            return
        assert self.runner is not None
        self._advance()
        self.runner.stop()
        self.updated.emit()

    def shutdown(self) -> None:
        """Abort an active ride (recorded as aborted). Idempotent."""
        self.stop()
        self._timer.stop()
        self._retry_unfinished()

    def tick(self) -> None:
        if self.is_active:
            self._advance()
        self.updated.emit()

    # --- listeners ---
    def _on_telemetry(self, telemetry: Telemetry) -> None:
        self._telemetry = telemetry

    def _on_heart_rate(self, reading: HeartRate) -> None:
        self._strap = reading
        self.updated.emit()

    def _on_hr_connection(self, state: ConnectionState) -> None:
        if state is not ConnectionState.CONNECTED:
            self._strap = None
        self.updated.emit()

    def _on_power(self, reading: PowerReading) -> None:
        self._meter = reading
        self.updated.emit()

    def _on_pm_connection(self, state: ConnectionState) -> None:
        if state is not ConnectionState.CONNECTED:
            self._meter = None
        self.updated.emit()

    def _on_connection(self, state: ConnectionState) -> None:
        if state is not ConnectionState.CONNECTED:
            if self.state is RunnerState.RUNNING:
                self.pause()
                if self.is_active:
                    self.error.emit(
                        "Trainer connection lost. Ride paused; resume after reconnecting."
                    )
            self._telemetry = None
        self.updated.emit()

    def _on_state(self, state: RunnerState) -> None:
        self.state_changed.emit(state)

    def _on_finished(self, completed: bool) -> None:
        self._sample(final=True)
        status = "completed" if completed else "aborted"
        self._ensure_recorder()
        recorder, self._recorder = self._recorder, None
        if recorder is None:
            if self._pending and self.workout is not None:
                self._unsaved.append(
                    (self.workout, self.ftp_w, self._pending, status, self._unit_id)
                )
                self._pending = []
            return
        if not self._guard(lambda: recorder.finish(status)):
            self._unfinished.append((recorder, status))
        self.session_finished.emit(recorder.session_id)

    # --- recording ---
    @property
    def kcal(self) -> float:
        """Food energy burnt: with ~24 % efficiency, kcal is about the same number as kJ."""
        return self.work_j / 1000

    @property
    def avg_power(self) -> float | None:
        return self._power_sum / self._power_n if self._power_n else None

    @property
    def avg_hr(self) -> float | None:
        return self._hr_sum / self._hr_n if self._hr_n else None

    def _clear_history(self) -> None:
        self.distance_m = 0.0
        self.work_j = 0.0
        self._power_sum = 0.0
        self._power_n = 0
        self._hr_sum = 0.0
        self._hr_n = 0
        self.history_t = []
        self.history_power = []

    def _advance(self) -> None:
        assert self.runner is not None
        now = self._clock()
        gap, self._last_advance = now - self._last_advance, now
        if gap > MAX_TICK_GAP_S and self.runner.state is RunnerState.RUNNING:
            self._skipped_s += gap  # nobody was riding; do not credit it
            self.pause()
            self.error.emit("The app was suspended. Ride paused.")
            return
        self.runner.tick()
        if self._match is not None and self.runner.state is RunnerState.RUNNING:
            trainer, meter = self._trainer_at(now), self._meter_at(now)
            self._match.update(trainer.power_w if trainer else None, meter.watts if meter else None)
        self._sample()

    def _ride_clock(self) -> float:
        return self._clock() - self._skipped_s

    def _sample(self, final: bool = False) -> None:
        """Record every second of ride_s up to now; a late tick back-fills missed ones."""
        runner = self.runner
        if runner is None or not (final or runner.state is RunnerState.RUNNING):
            return
        if not final:
            self._last_target = runner.target_w
        ride_s = runner.ride_s
        last = math.ceil(ride_s) - 1 if final else math.floor(ride_s)
        now = self._clock()
        while self._next_s <= last:
            ago = ride_s - self._next_s
            tel = self._telemetry_at(now - ago)
            sample = Sample(
                t=float(self._next_s),
                power=tel.power_w if tel else None,
                cadence=tel.cadence_rpm if tel else None,
                hr=self._hr_at(now - ago),
                target_w=self._last_target,
                speed=tel.speed_kph if tel else None,
                segment=self._segment,
            )
            self._next_s += 1
            self.distance_m += (sample.speed or 0.0) / 3.6  # 1 s per sample
            self.work_j += sample.power or 0
            if sample.power is not None:
                self._power_sum += sample.power
                self._power_n += 1
            if sample.hr is not None:
                self._hr_sum += sample.hr
                self._hr_n += 1
            self.history_t.append(max(0.0, runner.elapsed_s - ago))
            self.history_power.append(math.nan if sample.power is None else float(sample.power))
            self._record(sample)

    def _record(self, sample: Sample) -> None:
        if self._recorder is None:
            self._pending.append(sample)
            if len(self._pending) % FLUSH_INTERVAL_S == 0:
                self._ensure_recorder()
            return
        recorder = self._recorder
        self._guard(lambda: recorder.add(sample), clears_failure=False)
        # The recorder's own periodic flush hides errors, so flush just before it would.
        if (int(sample.t) + 1) % FLUSH_INTERVAL_S == 0:
            self._flush()

    def _flush(self) -> None:
        if self._recorder is not None:
            self._guard(self._recorder.flush)

    def _ensure_recorder(self) -> None:
        """Create the session; after a failure samples wait in _pending for the next attempt."""
        if self._recorder is not None or self.workout is None:
            return
        workout, unit_id = self.workout, self._unit_id

        def create() -> None:
            self._recorder = SessionRecorder(self._repo, workout, self.ftp_w, unit_id)

        if self._guard(create):
            pending, self._pending = self._pending, []
            for sample in pending:
                self._record(sample)

    def _retry_unfinished(self) -> None:
        unsaved, self._unsaved = self._unsaved, []
        for workout, ftp, samples, status, unit_id in unsaved:
            try:
                recorder = SessionRecorder(self._repo, workout, ftp, unit_id)
            except Exception:
                log.exception("saving a finished ride failed again")
                self._unsaved.append((workout, ftp, samples, status, unit_id))
                continue
            for sample in samples:
                recorder.add(sample)
            self._unfinished.append((recorder, status))
        pending, self._unfinished = self._unfinished, []
        for recorder, status in pending:
            try:
                recorder.finish(status)
            except Exception:
                log.exception("finishing session %d failed again", recorder.session_id)
                self._unfinished.append((recorder, status))

    def _guard(self, action: Callable[[], object], clears_failure: bool = True) -> bool:
        """Run a storage action; failures are reported (once per streak), never raised."""
        try:
            action()
        except Exception as exc:
            log.exception("storage failure")
            if not self._storage_failed:
                self.error.emit(f"Saving the ride failed (will retry): {exc}")
            self._storage_failed = True
            return False
        if clears_failure:
            self._storage_failed = False
        return True
