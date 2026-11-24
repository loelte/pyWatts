import json
import os
import subprocess
import sys
import threading
from datetime import date

import pytest

from pywatts.ai.mcp_stdio import PIPE_ENV, TOKEN_ENV
from pywatts.ai.prompt import COACH_PROMPT
from pywatts.ai.service import CoachService
from pywatts.ai.tools import ToolError, Tools
from pywatts.storage.db import open_db
from pywatts.storage.plan_repo import PlanRepo
from pywatts.storage.repo import Repo

DAY = date.today().isoformat()
UNIT = {"day": DAY, "kind": "rest", "title": "Rest"}
SEGMENTS = [{"type": "steady", "duration_s": 60, "power": 0.5}]


class FakeRides:
    is_active = False


@pytest.fixture
def rides():
    return FakeRides()


@pytest.fixture
def service(qapp, tmp_path, rides):
    conn = open_db(tmp_path / "a.db")
    service = CoachService(Repo(conn), PlanRepo(conn), rides, tmp_path / "new" / "coach")
    service.seen = []
    service.plan_changed.connect(lambda: service.seen.append("plan"))
    service.workouts_changed.connect(lambda: service.seen.append("workouts"))
    service.activity.connect(service.seen.append)
    yield service
    service.stop()
    conn.close()


def call(service, name, /, **arguments):
    return json.loads(service._handle("call", {"name": name, "arguments": arguments}))


def test_prepare(service, tmp_path):
    launch = service.prepare()
    assert set(launch.env) == {PIPE_ENV, TOKEN_ENV} and all(launch.env.values())
    assert launch.prompt == COACH_PROMPT
    text = launch.mcp_config.read_text("utf-8")
    config = json.loads(text)["mcpServers"]["pywatts"]
    assert config["type"] == "stdio" and config["args"] == ["-m", "pywatts.ai.mcp_stdio"]
    assert config["command"] == sys.executable
    assert launch.env[TOKEN_ENV] not in text
    assert service.prepare() == launch
    service.stop()
    service.stop()
    assert service.prepare().env[TOKEN_ENV] != launch.env[TOKEN_ENV]


def test_signals(service):
    made = call(service, "add_units", units=[UNIT])
    assert service.seen == ["add_units", "plan"]
    service.seen.clear()
    workout = call(service, "create_workout", name="W", segments=SEGMENTS)
    assert service.seen == ["create_workout", "plan", "workouts"]
    service.seen.clear()
    call(service, "undo_change", change_id=workout["change_id"])
    assert service.seen == ["undo_change", "plan", "workouts"]
    service.seen.clear()
    call(service, "get_profile")
    assert service.seen == ["get_profile"]
    service.seen.clear()
    with pytest.raises(ToolError):
        call(service, "delete_unit", unit_id="x" * 60)
    call(service, "delete_unit", unit_id=made["units"][0]["unit_id"])
    assert service.seen[0].startswith("delete_unit: There is no such unit")
    assert service.seen[1:] == ["delete_unit", "plan"]


def test_ride_blocks_calls_but_not_the_list(service, rides):
    rides.is_active = True
    for name in ("get_profile", "add_units", "nope", None):
        with pytest.raises(ToolError, match="ride is in progress"):
            service._handle("call", {"name": name, "arguments": {"units": [UNIT]}})
    assert "plan" not in service.seen
    assert len(service._handle("list", {"name": None, "arguments": None})) == 22
    rides.is_active = False
    call(service, "get_profile")


def test_failures_are_answered_without_details(service, monkeypatch):
    def fail(self, args):
        raise RuntimeError("C:\\Users\\someone\\a.db")

    monkeypatch.setattr(Tools, "_get_profile", fail)
    with pytest.raises(ToolError) as error:
        call(service, "get_profile")
    assert "internal error" in str(error.value) and "Users" not in str(error.value)
    assert all("Users" not in line for line in service.seen)
    call(service, "list_plans")


def test_activity_stays_short(service):
    for name in ("x" * 5000, "a\x1b[2J", None, ["a"], {}):
        with pytest.raises(ToolError):
            service._handle("call", {"name": name, "arguments": {}})
    assert all(len(line) <= 120 and line.isprintable() for line in service.seen)


def test_whole_way(qtbot, service):
    """The relay as the CLI would start it, against the real tools."""
    launch = service.prepare()
    config = json.loads(launch.mcp_config.read_text("utf-8"))["mcpServers"]["pywatts"]
    messages = [
        {"id": 1, "method": "tools/list"},
        {
            "id": 2,
            "method": "tools/call",
            "params": {"name": "add_units", "arguments": {"units": [UNIT]}},
        },
        {
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_calendar", "arguments": {"from": DAY, "to": DAY}},
        },
    ]
    data = b"".join(json.dumps(m).encode() + b"\n" for m in messages)
    result = []

    def run():
        result.append(
            subprocess.run(
                [config["command"], *config["args"]],
                input=data,
                capture_output=True,
                env=os.environ | launch.env,
                timeout=30,
            )
        )

    thread = threading.Thread(target=run)
    thread.start()
    qtbot.waitUntil(lambda: bool(result) or not thread.is_alive(), timeout=30_000)
    thread.join()
    listed, added, calendar = map(json.loads, result[0].stdout.splitlines())
    assert len(listed["result"]["tools"]) == 22
    assert not added["result"]["isError"]
    units = json.loads(calendar["result"]["content"][0]["text"])["units"]
    assert [(u["day"], u["title"]) for u in units] == [(DAY, "Rest")]
    assert "plan" in service.seen


def test_prompt_passes_through_a_cmd_wrapper():
    assert not set(COACH_PROMPT) & set("\"'\n\r%!^&|<>`$")
    assert COACH_PROMPT.isascii() and len(COACH_PROMPT) < 4000
