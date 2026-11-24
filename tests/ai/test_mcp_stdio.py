import io
import json
import os
import subprocess
import sys
import threading

import pytest

from pywatts.ai import mcp_stdio
from pywatts.ai.mcp_stdio import PIPE_ENV, PROTOCOLS, TOKEN_ENV, AppError, AppGone, handle_line
from pywatts.ai.server import ToolServer
from pywatts.ai.tools import ToolError

TOOLS = [{"name": "t", "description": "d", "inputSchema": {"type": "object"}}]


def fake_app(method, **fields):
    if method == "list":
        return TOOLS
    if fields["name"] == "gone":
        raise AppGone
    if fields["name"] == "bad":
        raise AppError("fix it")
    return json.dumps(fields["arguments"])


def gone(method, **fields):
    raise AppGone


def ask(message, app=fake_app):
    raw = message if isinstance(message, bytes) else json.dumps(message).encode()
    answer = handle_line(raw, app)
    return None if answer is None else json.loads(answer)


def request(method, params=None, uid=1):
    message = {"jsonrpc": "2.0", "id": uid, "method": method}
    return message if params is None else message | {"params": params}


@pytest.mark.parametrize("version", PROTOCOLS)
def test_initialize_echoes_known_versions(version):
    answer = ask(request("initialize", {"protocolVersion": version, "capabilities": {}}))
    assert answer["result"]["protocolVersion"] == version
    assert answer["result"]["capabilities"] == {"tools": {}}
    assert answer["result"]["serverInfo"]["name"] == "pywatts"


@pytest.mark.parametrize("params", [{"protocolVersion": "2099-01-01"}, {"protocolVersion": []}, {}])
def test_initialize_offers_the_newest(params):
    assert ask(request("initialize", params))["result"]["protocolVersion"] == "2025-11-25"


def test_methods():
    assert ask(request("ping", uid="x")) == {"jsonrpc": "2.0", "id": "x", "result": {}}
    assert ask(request("tools/list", {"cursor": "c"}))["result"] == {"tools": TOOLS}
    for method in ("server/discover", "resources/list", "prompts/list", "nope"):
        assert ask(request(method))["error"]["code"] == -32601
    assert ask(request("tools/list"), gone)["error"]["message"] == "pyWatts is not running"


def test_calls():
    result = ask(request("tools/call", {"name": "t", "arguments": {"ä": 1}}))["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"ä": 1}
    assert ask(request("tools/call", {"name": "t"}))["result"]["content"][0]["text"] == "{}"
    bad = ask(request("tools/call", {"name": "bad"}))["result"]
    assert bad == {"content": [{"type": "text", "text": "fix it"}], "isError": True}
    lost = ask(request("tools/call", {"name": "gone"}))["result"]
    assert lost["isError"] and lost["content"][0]["text"] == "pyWatts is not running"
    for params in ({}, {"name": 5}, {"name": "t", "arguments": []}, {"name": "t", "arguments": 1}):
        assert ask(request("tools/call", params))["error"]["code"] == -32602


def test_no_answer():
    assert ask({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert ask({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "t"}}) is None
    assert ask({"jsonrpc": "2.0", "id": 1, "result": {}}) is None
    assert ask([{"jsonrpc": "2.0", "method": "notifications/cancelled"}]) is None


@pytest.mark.parametrize("raw", [b"{", b"\xff", b"nope", b"[" * 100_000, b"9" * 9000])
def test_parse_errors(raw):
    assert ask(raw)["error"]["code"] == -32700 and ask(raw)["id"] is None


@pytest.mark.parametrize(
    "message",
    [5, "x", None, [], {}, {"id": 1}, {"id": None, "method": "ping"}]
    + [{"id": True, "method": "ping"}, {"id": [], "method": "ping"}, {"id": 1, "method": 5}],
)
def test_invalid_requests(message):
    assert ask(message)["error"]["code"] == -32600


def test_params_must_be_an_object():
    assert ask(request("ping", [1]))["error"]["code"] == -32602


def test_batch():
    answers = ask([request("ping"), 5, {"method": "notifications/x"}, request("nope", uid=2)])
    assert [a.get("error", {}).get("code") for a in answers] == [None, -32600, -32601]


def test_serve_survives_bad_lines(monkeypatch):
    monkeypatch.setattr(mcp_stdio, "MAX_LINE", 100)
    ping = json.dumps(request("ping")).encode()
    lines = [b"", b"x" * 100, b"y" * 1000, ping, b"  ", b"z" * 99, ping]
    sink = io.BytesIO()
    mcp_stdio.serve(io.BytesIO(b"\n".join(lines) + b"\r\n"), sink, fake_app)
    answers = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert ["error" in a for a in answers] == [True, True, False, True, False]
    sink = io.BytesIO()
    mcp_stdio.serve(io.BytesIO(b"x" * 1000), sink, fake_app)
    assert len(sink.getvalue().splitlines()) == 1


def test_app_not_running(monkeypatch):
    for path in ("", r"\\.\pipe\pywatts-nope" if sys.platform == "win32" else "/nope/sock"):
        monkeypatch.setenv(PIPE_ENV, path)
        with pytest.raises(AppGone):
            mcp_stdio.ask_app("list")
    monkeypatch.setenv(PIPE_ENV, "x")
    with pytest.raises(AppError, match="too large"):
        mcp_stdio.ask_app("call", arguments="x" * 2_000_000)


def test_imports_no_qt_and_no_database():
    code = (
        "import sys, pywatts.ai.mcp_stdio\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in ('PyQt6', 'sqlite3', 'bleak', "
        "'numpy', 'keyring') or m.startswith(('pywatts.storage', 'pywatts.core', 'pywatts.ui'))]\n"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)


class Process:
    """The real relay; answers are collected on a thread while the event loop runs."""

    def __init__(self, qtbot, env):
        self.qtbot = qtbot
        self.lines = []
        self.process = subprocess.Popen(
            [sys.executable, "-m", "pywatts.ai.mcp_stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # An unhelpful code page: the relay must not depend on it.
            env=os.environ | {"PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"} | env,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            self.lines.append(line)

    def ask(self, message):
        before = len(self.lines)
        self.process.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
        self.process.stdin.flush()
        self.qtbot.waitUntil(lambda: len(self.lines) > before, timeout=20_000)
        return json.loads(self.lines[-1])

    def text(self, **params):
        result = self.ask(request("tools/call", params))["result"]
        return result["content"][0]["text"], result["isError"]

    def close(self):
        self.process.stdin.close()
        self.process.wait(20)
        self.reader.join(5)
        error = self.process.stderr.read()
        self.process.stdout.close()
        self.process.stderr.close()
        return self.process.returncode, error


def test_real_process(qtbot):
    calls = []

    def handler(method, params):
        if method == "list":
            return TOOLS
        calls.append((params["name"], params["arguments"]))
        if params["name"] == "bad":
            raise ToolError("fix it – ü")
        return "ü" * 30_000

    server = ToolServer(handler)
    server.start()
    process = Process(qtbot, {PIPE_ENV: server.name, TOKEN_ENV: server.token})
    try:
        assert process.ask(request("server/discover"))["error"]["code"] == -32601
        assert "result" in process.ask(request("initialize", {"protocolVersion": "2025-06-18"}))
        assert process.ask(request("tools/list"))["result"]["tools"] == TOOLS
        assert process.text(name="t", arguments={"title": "Zürich ⚡"}) == ("ü" * 30_000, False)
        assert calls == [("t", {"title": "Zürich ⚡"})]
        assert process.text(name="bad") == ("fix it – ü", True)
        for _ in range(10):  # more connections than may be open at once
            process.text(name="t")
        qtbot.waitUntil(lambda: server.client_count == 0)
        server.stop()
        assert process.text(name="t") == ("pyWatts is not running", True)
        assert "error" in process.ask(request("tools/list"))
        assert "result" in process.ask(request("ping"))
    finally:
        server.stop()
        code, error = process.close()
    assert (code, error) == (0, b"")


def test_real_process_with_a_wrong_token(qtbot):
    calls = []
    server = ToolServer(lambda method, params: calls.append(method))
    server.start()
    process = Process(qtbot, {PIPE_ENV: server.name, TOKEN_ENV: "wrong"})
    try:
        assert process.text(name="t") == ("The token is wrong.", True)
    finally:
        server.stop()
        process.close()
    assert calls == []
