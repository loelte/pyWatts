"""BLE loop thread + Qt bridge. The only place where BLE and GUI threads meet."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from pywatts.trainer.base import (
    ConnectionState,
    HeartRateEvent,
    HeartRateMonitor,
    Trainer,
    TrainerError,
    TrainerEvent,
)

log = logging.getLogger(__name__)

_DEFAULT_SHUTDOWN_S = 5.0


class _Worker:
    """BLE side. No Qt; hands events to `post`, which is never called once closed."""

    def __init__(
        self,
        factory: Callable[[], Trainer],
        post: Callable[[str, object], Any],
        hr_factory: Callable[[], HeartRateMonitor] | None = None,
    ) -> None:
        self._factory = factory
        self._hr_factory = hr_factory
        self._post = post
        self._post_lock = threading.Lock()
        self._closed = False
        self._budget = _DEFAULT_SHUTDOWN_S
        self._loop = asyncio.new_event_loop()
        # Below: loop thread only.
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._queue: deque[tuple[str, tuple[Any, ...]]] = deque()
        self._trainer: Trainer | None = None
        self._current: asyncio.Task[Any] | None = None
        self._current_name = ""
        self._scan: asyncio.Task[Any] | None = None
        self._hr: HeartRateMonitor | None = None
        self._hr_tasks: dict[str, asyncio.Task[Any]] = {}  # "scan" and "link"
        self._thread = threading.Thread(target=self._thread_main, name="ble", daemon=True)
        self._thread.start()

    # any thread

    def submit(self, name: str, *args: Any) -> None:
        if not self._closed:
            self._call(self._enqueue, name, args)

    def submit_hr(self, name: str, *args: Any) -> None:
        if not self._closed:
            self._call(self._enqueue_hr, name, args)

    def close(self, budget: float = _DEFAULT_SHUTDOWN_S) -> bool:
        """Stop posting and begin shutting down. False if already closed."""
        with self._post_lock:
            if self._closed:
                return False
            self._closed = True
        self._budget = budget
        self._call(self._stop.set)
        return True

    def join(self, timeout: float) -> bool:
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def _call(self, fn: Callable[..., Any], *args: Any) -> None:
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # loop closed
            pass

    def post(self, kind: str, payload: object) -> None:
        with self._post_lock:  # close() waits for an emission in progress
            if self._closed:
                return
            try:
                self._post(kind, payload)
            except RuntimeError:  # C++ object deleted
                self._closed = True

    # loop thread

    def _thread_main(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception:
            log.exception("BLE thread failed")
        finally:
            self._loop.close()

    async def _main(self) -> None:
        try:
            self._trainer = self._factory()
            self._trainer.add_listener(self._on_event)
        except Exception as e:
            log.exception("trainer factory failed")
            self._trainer = None
            self.post("error", f"Trainer unavailable: {e}")
        if self._hr_factory is not None:
            try:
                self._hr = self._hr_factory()
                self._hr.add_listener(self._on_hr_event)
            except Exception as e:
                log.exception("heart rate factory failed")
                self._hr = None
                self.post("hr_error", f"Heart rate unavailable: {e}")
        runner = asyncio.ensure_future(self._run())
        await self._stop.wait()

        deadline = time.monotonic() + self._budget
        hr_tasks = self._hr_tasks.values()
        doomed = {t for t in (runner, self._current, self._scan, *hr_tasks) if t is not None}
        await self._finish(doomed, self._budget * 0.25)
        hr_close = asyncio.ensure_future(self._close_hr())  # beside the trainer's shutdown
        if self._trainer is not None:
            for fn, share in ((self._trainer.stop, 0.3), (self._trainer.disconnect, 0.3)):
                await self._bounded(fn(), self._budget * share)
        await asyncio.wait({hr_close}, timeout=max(0.0, (deadline - time.monotonic()) * 0.5))
        left = asyncio.all_tasks() - {asyncio.current_task()}
        await self._finish(left, max(0.0, (deadline - time.monotonic()) * 0.5))

    @staticmethod
    async def _finish(tasks: set[Any], timeout: float) -> None:
        for t in tasks:
            t.cancel()
        if tasks:
            done, _ = await asyncio.wait(tasks, timeout=timeout)
            for t in done:
                if not t.cancelled():
                    t.exception()

    async def _bounded(self, coro: Coroutine[Any, Any, None], timeout: float) -> None:
        # Not wait_for: that would wait for a task that ignores its cancellation.
        task = asyncio.ensure_future(coro)
        await asyncio.wait({task}, timeout=timeout)
        if not task.done():
            task.cancel()
        elif not task.cancelled() and (e := task.exception()) is not None:
            log.info("shutdown: %s", e)

    def _on_event(self, event: TrainerEvent) -> None:
        self.post("state" if isinstance(event, ConnectionState) else "telemetry", event)

    # The strap has its own tasks, so it can never delay trainer control.

    async def _close_hr(self) -> None:
        if self._hr is not None:
            try:
                await self._hr.disconnect()
            except Exception as e:
                log.info("strap shutdown: %s", e)

    def _on_hr_event(self, event: HeartRateEvent) -> None:
        self.post("hr_state" if isinstance(event, ConnectionState) else "hr", event)

    def _enqueue_hr(self, name: str, args: tuple[Any, ...]) -> None:
        if self._stop.is_set():
            return
        slot = "scan" if name == "scan" else "link"
        previous = self._hr_tasks.get(slot)
        if previous is not None and name != "scan":
            previous.cancel()  # a newer connect/disconnect replaces a pending one
        self._hr_tasks[slot] = asyncio.ensure_future(self._do_hr(previous, name, args))

    async def _do_hr(
        self, previous: asyncio.Task[Any] | None, name: str, args: tuple[Any, ...]
    ) -> None:
        devices: list[Any] = []
        try:
            if previous is not None and not previous.done():
                await asyncio.wait({previous})
            if self._hr is None:
                raise TrainerError("Heart rate straps are not available")
            result = await getattr(self._hr, name)(*args)
            devices = list(result or [])
        except asyncio.CancelledError as e:
            task = asyncio.current_task()
            if task is None or not task.cancelling():
                self.post("hr_error", f"Bluetooth operation was aborted: {name} {e}".strip())
        except TrainerError as e:
            self.post("hr_error", str(e))
        except Exception as e:
            log.exception("unexpected strap error")
            self.post("hr_error", f"Unexpected strap error: {e or type(e).__name__}")
        if name == "scan":
            self.post("hr_scan", devices)

    def _enqueue(self, name: str, args: tuple[Any, ...]) -> None:
        if name in ("set_target_power", "set_open_mode"):
            self._drop_queued("set_target_power")
            self._drop_queued("set_open_mode")
        elif name == "disconnect":
            self._drop_queued("connect")
            if self._current is not None and self._current_name == "connect":
                self._current.cancel()
        self._queue.append((name, args))
        self._wake.set()

    def _drop_queued(self, name: str) -> None:
        kept = [c for c in self._queue if c[0] != name]
        self._queue.clear()
        self._queue.extend(kept)

    async def _run(self) -> None:
        while True:
            while not self._queue:
                self._wake.clear()
                await self._wake.wait()
            name, args = self._queue.popleft()
            if name == "scan":
                # Not awaited, so a scan cannot hold up control commands during a ride.
                previous = self._scan
                self._scan = asyncio.ensure_future(self._do_scan(previous, *args))
                await asyncio.sleep(0)  # let it start before the next command
                continue
            self._current_name = name
            self._current = asyncio.ensure_future(self._do(name, args))
            await asyncio.wait({self._current})
            self._current = None

    async def _do_scan(self, previous: asyncio.Task[Any] | None, timeout: float) -> None:
        if previous is not None and not previous.done():
            await asyncio.wait({previous})
        devices: list[Any] = []
        try:
            devices = list(await self._need_trainer().scan(timeout))
        except Exception as e:
            self._report(e)
        self.post("scan", devices)

    async def _do(self, name: str, args: tuple[Any, ...]) -> None:
        try:
            trainer = self._need_trainer()
            if name == "connect" and self._scan is not None and not self._scan.done():
                await asyncio.wait({self._scan})
            await getattr(trainer, name)(*args)
        except asyncio.CancelledError as e:
            task = asyncio.current_task()
            if task is None or not task.cancelling():  # raised by the BLE stack, not by us
                self._report(TrainerError(f"Bluetooth operation was aborted: {name} {e}".strip()))
        except Exception as e:
            self._report(e)

    def _need_trainer(self) -> Trainer:
        if self._trainer is None:
            raise TrainerError("Trainer unavailable")
        return self._trainer

    def _report(self, e: Exception) -> None:
        if isinstance(e, TrainerError):
            self.post("error", str(e))
        else:
            log.error("unexpected trainer error", exc_info=e)
            self.post("error", f"Unexpected trainer error: {e or type(e).__name__}")


class TrainerBridge(QObject):
    """Owns a daemon thread with its own asyncio loop. Satisfies core.runner.TrainerControl.

    The trainer is built by `factory` ON the BLE thread. All public methods are called from the
    GUI thread, never block (except shutdown, bounded), and report failure through `error`.
    Commands execute in call order; a pending set_target_power/set_open_mode is
    replaced by a newer one.
    Signals are delivered on the GUI thread. `state` is the last state as seen by the GUI thread.
    """

    telemetry = pyqtSignal(object)  # Telemetry
    connection_changed = pyqtSignal(object)  # ConnectionState
    scan_finished = pyqtSignal(list)  # list[DeviceInfo]
    error = pyqtSignal(str)

    hr_reading = pyqtSignal(object)  # HeartRate
    hr_connection_changed = pyqtSignal(object)  # ConnectionState
    hr_scan_finished = pyqtSignal(list)  # list[DeviceInfo]
    hr_error = pyqtSignal(str)

    _posted = pyqtSignal(str, object)  # BLE thread -> GUI thread

    def __init__(
        self,
        factory: Callable[[], Trainer],
        parent: QObject | None = None,
        hr_factory: Callable[[], HeartRateMonitor] | None = None,
    ) -> None:
        super().__init__(parent)
        self.state = ConnectionState.DISCONNECTED
        self.hr_state = ConnectionState.DISCONNECTED
        self._shut_down = False
        self._posted.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)
        worker = self._worker = _Worker(factory, self._posted.emit, hr_factory)
        # Deleted without shutdown(): a lambda because it must not be bound to this object.
        self.destroyed.connect(lambda: worker.close() and None)

    @pyqtSlot(str, object)
    def _dispatch(self, kind: str, payload: object) -> None:
        if self._shut_down:
            return
        if kind == "telemetry":
            self.telemetry.emit(payload)
        elif kind == "state":
            assert isinstance(payload, ConnectionState)
            self.state = payload
            self.connection_changed.emit(payload)
        elif kind == "scan":
            self.scan_finished.emit(payload)
        elif kind == "hr":
            self.hr_reading.emit(payload)
        elif kind == "hr_state":
            assert isinstance(payload, ConnectionState)
            self.hr_state = payload
            self.hr_connection_changed.emit(payload)
        elif kind == "hr_scan":
            self.hr_scan_finished.emit(payload)
        elif kind == "hr_error":
            self.hr_error.emit(str(payload))
        else:
            self.error.emit(str(payload))

    def scan(self, timeout: float = 5.0) -> None:
        self._worker.submit("scan", timeout)

    def connect_trainer(self, addr: str) -> None:
        self._worker.submit("connect", addr)

    def disconnect_trainer(self) -> None:
        self._worker.submit("disconnect")

    def hr_scan(self, timeout: float = 5.0) -> None:
        self._worker.submit_hr("scan", timeout)

    def hr_connect(self, addr: str) -> None:
        self._worker.submit_hr("connect", addr)

    def hr_disconnect(self) -> None:
        self._worker.submit_hr("disconnect")

    def set_target_power(self, w: int) -> None:
        self._worker.submit("set_target_power", w)

    def set_open_mode(self) -> None:
        self._worker.submit("set_open_mode")

    def start(self) -> None:
        self._worker.submit("start")

    def pause(self) -> None:
        self._worker.submit("pause")

    def stop(self) -> None:
        self._worker.submit("stop")

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop + disconnect trainer, end loop, join thread. Idempotent."""
        if self._shut_down:
            return
        self._shut_down = True
        self._worker.close(timeout)
        if not self._worker.join(timeout):
            log.warning("BLE thread did not end within %.1f s", timeout)
        self.state = ConnectionState.DISCONNECTED
        self.hr_state = ConnectionState.DISCONNECTED
