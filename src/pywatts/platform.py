"""The only place for OS-specific code and OS-dependent locations."""

import os
import sys
from pathlib import Path

from platformdirs import user_config_path, user_data_path, user_documents_path


def set_taskbar_identity() -> None:
    """Without its own ID, Windows shows the python.exe icon in the taskbar."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("pyWatts")


def data_dir() -> Path:
    return user_data_path("pywatts", appauthor=False, ensure_exists=True)


def config_dir() -> Path:
    return user_config_path("pywatts", appauthor=False, ensure_exists=True)


def db_path() -> Path:
    return data_dir() / "pywatts.db"


def coach_dir() -> Path:
    """Working directory of the AI program: without project files it could pick up."""
    path = data_dir() / "coach"
    path.mkdir(exist_ok=True)
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
