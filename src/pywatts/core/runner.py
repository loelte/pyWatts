"""Workout execution state machine. Pure Python, single-threaded (GUI thread)."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import replace
from enum import Enum
from typing import Protocol

from pywatts.core.workout import Block, Workout, flatten, power_at

RAMP_IN_S = 5.0


class RunnerState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"


class TrainerControl(Protocol):
    """Non-blocking sync command sink. Implemented by trainer.thread.TrainerBridge."""

    def set_target_power(self, w: int) -> None: ...
    def set_open_mode(self) -> None: ...
    def start(self) -> None: ...
    def pause(self) -> None: ...
    def stop(self) -> None: ...


class Runner:
    """Driven by tick() at ~1 Hz; elapsed time comes from clock(), so late ticks are fine.

    Public attrs/properties:
      state, blocks (list[Block]; mutated copy after extend), block_index,
      elapsed_s  (position on workout timeline, jumps on skip),
      ride_s     (active riding time, monotonic, excludes pauses),
      remaining_s, block_remaining_s, target_w (int; 0 unless RUNNING/PAUSED)
    Listener lists (append callables):
      target_changed(target_w), block_changed(block_index),
      state_changed(RunnerState), finished(completed: bool)
    Rules:
      start(): IDLE->RUNNING; trainer.start() then set_target_power. Empty workout -> ValueError.
      pause(): trainer.pause(). resume(): trainer.start(), then ramp-in: target scales linearly
        50%->100% over RAMP_IN_S of riding time. Commands invalid in a state are no-ops.
      skip_block(): jump to next block's t0; on last block -> DONE, finished(True).
      adjust_power(w): add w watts to every target from now on (offset_w; target never < 0).
      extend(s): lengthen current block (its t1 and all later blocks shift).
      stop(): -> DONE, trainer.stop(), finished(False).
      End of timeline -> DONE, trainer.stop(), finished(True). finished fires exactly once.
      set_target_power is sent only on change (and always after start/resume).
      Open blocks (sprints) get set_open_mode instead; target_w is 0 there.
    """

    def __init__(
        self,
        workout: Workout,
        ftp_w: int,
        trainer: TrainerControl,
        clock: Callable[[], float],
    ) -> None:
        self.state = RunnerState.IDLE
        self.blocks: list[Block] = flatten(workout)
        self.block_index = 0
        self.target_changed: list[Callable[[int], None]] = []
        self.block_changed: list[Callable[[int], None]] = []
        self.state_changed: list[Callable[[RunnerState], None]] = []
        self.finished: list[Callable[[bool], None]] = []
        self._ftp_w = ftp_w
        self._trainer = trainer
        self._clock = clock
        # All times below are as of the last sync (tick or command).
        self._ride = 0.0
        self._elapsed = 0.0
        # Running since clock time _c0, at which ride_s was _r0.
        self._c0 = 0.0
        self._r0 = 0.0
        # elapsed_s was _e1 when ride_s was _r1 (re-anchored on skip).
        self._e1 = 0.0
        self._r1 = 0.0
        self._ramp_from: float | None = None  # ride_s at resume
        self._target = 0
        self._offset_w = 0
        self._sent: int | str | None = None  # "open" = open mode
        # Mutations happen at once; notifications are queued and delivered in order by the
        # outermost call, so listeners always see consistent state and may call any method.
        self._events: deque[tuple[list[Callable], object]] = deque()
        self._dispatching = False

    @property
    def elapsed_s(self) -> float:
        return self._elapsed

    @property
    def ride_s(self) -> float:
        return self._ride

    @property
    def remaining_s(self) -> float:
        return max(0.0, self._total - self._elapsed)

    @property
    def block_remaining_s(self) -> float:
        if not self.blocks:
            return 0.0
        return max(0.0, self.blocks[self.block_index].t1 - self._elapsed)

    @property
    def offset_w(self) -> int:
        return self._offset_w

    @property
    def target_w(self) -> int:
        return self._target

    @property
    def open_mode(self) -> bool:
        """The current block is ridden without ERG."""
        return self._active and self.blocks[self.block_index].open

    @property
    def next_block(self) -> Block | None:
        i = self.block_index + 1
        return self.blocks[i] if i < len(self.blocks) else None

    @property
    def target_rpm(self) -> int | None:
        """Cadence target of the current block; None if it has none or nothing runs."""
        if self.state not in (RunnerState.RUNNING, RunnerState.PAUSED) or not self.blocks:
            return None
        return self.blocks[self.block_index].cadence

    def start(self) -> None:
        if self.state is not RunnerState.IDLE:
            return
        if not self.blocks:
            raise ValueError("workout is empty")
        self._run(ramp_in=False)

    def pause(self) -> None:
        self._sync()
        if self.state is RunnerState.RUNNING:
            self._set_state(RunnerState.PAUSED)
            self._update_target()
            self._trainer.pause()
        self._dispatch()

    def resume(self) -> None:
        if self.state is RunnerState.PAUSED:
            self._run(ramp_in=True)

    def skip_block(self) -> None:
        self._sync()
        if self._active:
            if self.block_index == len(self.blocks) - 1:
                self._move_to(self._total)
                self._finish(True)
            else:
                self._move_to(self.blocks[self.block_index + 1].t0)
                self._enter_block()
                self._update_target()
        self._dispatch()

    def extend(self, seconds: int = 30) -> None:
        self._sync()
        if self._active and seconds > 0:
            i = self.block_index
            self.blocks[i] = replace(self.blocks[i], t1=self.blocks[i].t1 + seconds)
            for j in range(i + 1, len(self.blocks)):
                b = self.blocks[j]
                self.blocks[j] = replace(b, t0=b.t0 + seconds, t1=b.t1 + seconds)
            self._update_target()
        self._dispatch()

    def adjust_power(self, delta_w: int) -> None:
        """Shift the target of the whole workout by delta_w watts (accumulates)."""
        self._sync()
        if self._active and delta_w:
            self._offset_w += delta_w
            self._update_target()
        self._dispatch()

    def stop(self) -> None:
        self._sync()
        if self._active:
            self._finish(False)
        self._dispatch()

    def tick(self) -> None:
        self._sync()
        if self.state is RunnerState.RUNNING:
            self._update_target()
        self._dispatch()

    @property
    def _active(self) -> bool:
        return self.state in (RunnerState.RUNNING, RunnerState.PAUSED)

    @property
    def _total(self) -> float:
        return self.blocks[-1].t1 if self.blocks else 0.0

    def _set_state(self, state: RunnerState) -> None:
        self.state = state
        self._events.append((self.state_changed, state))

    def _run(self, ramp_in: bool) -> None:
        self._c0 = self._clock()
        self._r0 = self._ride
        self._ramp_from = self._ride if ramp_in else None
        self._set_state(RunnerState.RUNNING)
        self._sent = None
        self._trainer.start()
        self._update_target()
        self._dispatch()

    def _sync(self) -> None:
        """Bring time and block up to the clock; finish if the timeline is over."""
        if self.state is not RunnerState.RUNNING:
            return
        self._ride = max(self._ride, self._r0 + self._clock() - self._c0)
        self._elapsed = self._e1 + self._ride - self._r1
        self._enter_block()
        overshoot = self._elapsed - self._total
        if overshoot >= 0:
            self._ride -= overshoot  # a late tick must not count time past the end
            self._elapsed = self._total
            self._finish(True)

    def _move_to(self, elapsed: float) -> None:
        self._elapsed = self._e1 = elapsed
        self._r1 = self._ride

    def _enter_block(self) -> None:
        """Advance to the block at elapsed_s; a jump over several blocks notifies once."""
        i = self.block_index
        while i < len(self.blocks) - 1 and self._elapsed >= self.blocks[i].t1:
            i += 1
        if i != self.block_index:
            self.block_index = i
            self._events.append((self.block_changed, i))

    def _update_target(self) -> None:
        target = 0
        if self._active:
            watts = power_at(self.blocks, self._elapsed) * self._ftp_w
            if self._ramp_from is not None:
                done = (self._ride - self._ramp_from) / RAMP_IN_S
                if done >= 1.0:
                    self._ramp_from = None
                else:
                    watts *= 0.5 + 0.5 * done
            target = 0 if self.open_mode else max(0, int(watts + 0.5) + self._offset_w)
        if target != self._target:
            self._target = target
            self._events.append((self.target_changed, target))
        command = "open" if self.open_mode else target
        if self.state is RunnerState.RUNNING and command != self._sent:
            self._sent = command
            if self.open_mode:
                self._trainer.set_open_mode()
            else:
                self._trainer.set_target_power(target)

    def _finish(self, completed: bool) -> None:
        self._set_state(RunnerState.DONE)
        self._update_target()
        self._events.append((self.finished, completed))
        self._trainer.stop()

    def _dispatch(self) -> None:
        """Deliver queued events. A raising listener does not starve the others."""
        if self._dispatching:
            return
        self._dispatching = True
        error: Exception | None = None
        try:
            while self._events:
                listeners, arg = self._events.popleft()
                for listener in list(listeners):
                    try:
                        listener(arg)
                    except Exception as e:
                        error = error or e
        finally:
            self._dispatching = False
        if error is not None:
            raise error
