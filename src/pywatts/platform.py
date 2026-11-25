"""The only place for OS-specific code and OS-dependent locations."""

import asyncio
import logging
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

from platformdirs import user_config_path, user_data_path, user_documents_path

log = logging.getLogger(__name__)

APP_ID = "pywatts"  # name of the .desktop file, see scripts/install_linux.sh
_MAC = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", re.IGNORECASE)
_TOOL_TIMEOUT_S = 5.0


def set_taskbar_identity() -> None:
    """Without its own ID, Windows shows the python.exe icon in the taskbar."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("pyWatts")


def set_full_speed(on: bool) -> None:
    """Windows 11 may run a process without a visible window on slow cores: building the window
    then takes several times as long. on: not for us; off: Windows decides again."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class Throttling(ctypes.Structure):  # PROCESS_POWER_THROTTLING_STATE
        _fields_ = [
            ("version", wintypes.ULONG),
            ("control_mask", wintypes.ULONG),
            ("state_mask", wintypes.ULONG),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessInformation.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        state = Throttling(1, 1 if on else 0, 0)  # execution speed: controlled, not throttled
        if not kernel32.SetProcessInformation(
            kernel32.GetCurrentProcess(),
            4,  # ProcessPowerThrottling
            ctypes.byref(state),
            ctypes.sizeof(state),
        ):
            log.info("power throttling: error %d", ctypes.get_last_error())
    except (OSError, AttributeError) as e:  # before Windows 10 1709
        log.info("power throttling: %s", e)


def on_termination(quit_app: Callable[[], object]) -> bool:
    """Call quit_app when the session ends, the terminal closes or on Ctrl+C: BlueZ keeps a
    trainer connected, with its load, if we just die. False: not needed on this system.

    Python handlers run only while Python code does, so a Qt loop needs a timer."""
    if sys.platform == "win32":
        return False
    import signal

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda _number, _frame: quit_app())
    return True


async def release_bluetooth_link(addr: str) -> bool:
    """End a link that an earlier process left behind. True if there was one. Linux only:
    elsewhere a link ends with its process."""
    if not sys.platform.startswith("linux") or not _MAC.fullmatch(addr):
        return False
    if "Connected: yes" not in await _bluetoothctl("info", addr):
        return False
    log.warning("ending a left-over Bluetooth link")
    await _bluetoothctl("disconnect", addr)
    return True


async def _bluetoothctl(*args: str) -> str:
    """Its output; "" if it fails or does not answer (it waits for a missing service)."""
    process = None
    try:
        async with asyncio.timeout(_TOOL_TIMEOUT_S):
            process = await asyncio.create_subprocess_exec(
                "bluetoothctl",
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output, _ = await process.communicate()
        return output.decode("utf-8", "replace")
    except (OSError, TimeoutError) as e:
        log.info("bluetoothctl: %s", e or type(e).__name__)
        return ""
    finally:
        if process is not None and process.returncode is None:
            process.kill()


def data_dir() -> Path:
    return user_data_path("pywatts", appauthor=False, ensure_exists=True)


def config_dir() -> Path:
    return user_config_path("pywatts", appauthor=False, ensure_exists=True)


def db_path() -> Path:
    return data_dir() / "pywatts.db"


def lock_path() -> Path:
    """Held by the only running pyWatts that may use a trainer."""
    return data_dir() / "pywatts.lock"


def coach_dir(rider_id: str) -> Path:
    """Working directory of the AI program: without project files it could pick up. One per
    rider, because such programs keep their conversations by directory."""
    path = data_dir() / "coach" / rider_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def zwift_activity_dirs() -> list[Path]:
    """Existing folders; Zwift moved to AppData in 2026 and left older files in Documents."""
    roots = [user_documents_path()]
    if local := os.environ.get("LOCALAPPDATA"):
        roots.insert(0, Path(local))
    if onedrive := os.environ.get("OneDrive"):
        roots.append(Path(onedrive) / "Documents")
    found = [root / "Zwift" / "Activities" for root in dict.fromkeys(roots)]
    return [path for path in found if path.is_dir()]
