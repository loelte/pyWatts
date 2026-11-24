"""Rules for the AI coach: when tools may run, and who is told about changes. GUI thread."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PyQt6.QtCore import QObject, pyqtSignal

from pywatts.ai.mcp_stdio import PIPE_ENV, TOKEN_ENV
from pywatts.ai.prompt import COACH_PROMPT
from pywatts.ai.server import ToolServer
from pywatts.ai.tools import ToolError, Tools
from pywatts.storage.plan_repo import PlanRepo
from pywatts.storage.repo import Repo

log = logging.getLogger(__name__)

CONFIG_NAME = "mcp.json"
SERVER_NAME = "pywatts"
_SHORT = 120


class RideSource(Protocol):
    """RideController."""

    @property
    def is_active(self) -> bool: ...


@dataclass(frozen=True)
class Launch:
    """What a CLI needs. env goes into its environment; the CLI must pass it on to the MCP
    server (Claude Code does; a custom one may not, then the tools answer "not running")."""

    env: dict[str, str]
    mcp_config: Path
    prompt: str


def _python() -> str:
    """With stdio, also if the app itself runs without a console."""
    path = Path(sys.executable)
    if path.name.lower() == "pythonw.exe" and (console := path.with_name("python.exe")).exists():
        path = console
    return str(path)


class CoachService(QObject):
    plan_changed = pyqtSignal()  # plans, units or the workouts in them
    workouts_changed = pyqtSignal()  # the library
    activity = pyqtSignal(str)  # one short line per tool call

    def __init__(
        self,
        repo: Repo,
        plans: PlanRepo,
        rides: RideSource,
        config_dir: Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._rides = rides
        self._config_dir = config_dir
        self._tools = Tools(repo, plans)
        self._server = ToolServer(self._handle, self)

    def prepare(self) -> Launch:
        """Start the server if needed and write the config file. OSError if that fails."""
        self._server.start()
        self._config_dir.mkdir(parents=True, exist_ok=True)
        path = self._config_dir / CONFIG_NAME
        # The token stays out of the file.
        server = {"type": "stdio", "command": _python(), "args": ["-m", "pywatts.ai.mcp_stdio"]}
        path.write_text(json.dumps({"mcpServers": {SERVER_NAME: server}}, indent=2), "utf-8")
        env = {PIPE_ENV: self._server.name, TOKEN_ENV: self._server.token}
        return Launch(env, path, COACH_PROMPT)

    def stop(self) -> None:
        """Also makes the last Launch useless."""
        self._server.stop()

    def _handle(self, method: str, params: dict[str, Any]) -> object:
        if method == "list":
            return self._tools.definitions()
        name = params["name"] if isinstance(params["name"], str) else "?"
        name = "".join(c if c.isprintable() else "?" for c in name[:40])
        try:
            result = self._call(params)
        except ToolError as e:
            self.activity.emit(f"{name}: {e}"[:_SHORT])
            raise
        changed = self._tools.changed
        self.activity.emit(name)
        if changed:
            self.plan_changed.emit()
        if changed.workouts:
            self.workouts_changed.emit()
        return result

    def _call(self, params: dict[str, Any]) -> str:
        if self._rides.is_active:
            raise ToolError("A ride is in progress; try again afterwards.")
        try:
            return self._tools.call(params["name"], params["arguments"])
        except ToolError:
            raise
        except Exception as e:
            log.exception("tool call failed")
            raise ToolError("pyWatts could not do this because of an internal error.") from e
