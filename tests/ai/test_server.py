import json

import pytest
from PyQt6.QtNetwork import QLocalSocket

from pywatts.ai import server as server_module
from pywatts.ai.server import MAX_CLIENTS, MAX_LINE, ToolServer
from pywatts.ai.tools import ToolError


class Handler:
    def __init__(self):
        self.calls = []
        self.answer = "ok"

    def __call__(self, method, params):
        self.calls.append((method, params["name"], params["arguments"]))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def handler():
    return Handler()


@pytest.fixture
def server(qapp, handler):
    server = ToolServer(handler)
    server.start()
    yield server
    server.stop()


class Client:
    def __init__(self, qtbot, server):
        self.qtbot = qtbot
        self.token = server.token
        self.socket = QLocalSocket()
        self.received = b""
        self.socket.readyRead.connect(self._read)
        self.socket.connectToServer(server.name)
        assert self.socket.waitForConnected(2000)

    def _read(self):
        self.received += self.socket.readAll().data()

    def send(self, raw):
        self.socket.write(raw)
        self.socket.flush()

    def ask(self, **fields):
        self.send(json.dumps({"token": self.token, "id": 7} | fields).encode() + b"\n")
        return self.answer()

    def answer(self):
        self.qtbot.waitUntil(lambda: b"\n" in self.received)
        line, _, self.received = self.received.partition(b"\n")
        return json.loads(line)

    @property
    def open(self):
        return self.socket.state() == QLocalSocket.LocalSocketState.ConnectedState

    def wait_closed(self):
        self.qtbot.waitUntil(lambda: not self.open)


@pytest.fixture
def connect(qtbot, server):
    return lambda: Client(qtbot, server)


def test_requests(connect, handler):
    client = connect()
    assert client.ask(method="call", name="n", arguments={"a": 1}) == {"id": 7, "result": "ok"}
    handler.answer = [{"name": "x"}]
    assert client.ask(method="list") == {"id": 7, "result": [{"name": "x"}]}
    assert handler.calls == [("call", "n", {"a": 1}), ("list", None, None)]
    handler.answer = ToolError("fix it")
    assert client.ask(method="call") == {"id": 7, "error": "fix it"}
    handler.answer = RuntimeError("C:\\secret")
    assert client.ask(method="call") == {"id": 7, "error": "internal error"}
    handler.answer = object()
    assert client.ask(method="call")["error"] == "internal error"
    assert client.ask(method="quit")["error"] == "unknown method"
    assert client.ask(method=["list"])["error"] == "unknown method"
    assert client.open


def test_partial_and_joined_lines(qtbot, connect, handler):
    client = connect()
    line = json.dumps({"token": client.token, "id": "a", "method": "list"}).encode() + b"\n"
    client.send(line[:10])
    qtbot.wait(30)
    client.send(line[10:] + line + line[:5])
    assert client.answer()["id"] == "a" and client.answer()["id"] == "a"
    client.send(line[5:])
    assert client.answer()["id"] == "a"
    assert len(handler.calls) == 3


def test_large_answer(connect, handler):
    handler.answer = "x" * 2_000_000
    assert len(connect().ask(method="call")["result"]) == 2_000_000


@pytest.mark.parametrize("token", ["", "x", "ü\ud800", None, 5, True, [], {}])
def test_wrong_token(connect, handler, token):
    client = connect()
    answer = client.ask(method="call", token=token)
    assert answer == {"id": 7, "error": "The token is wrong."}
    client.token = ""
    client.send(json.dumps({"token": "x", "method": "call"}).encode() + b"\n")
    client.wait_closed()
    assert handler.calls == []


def test_missing_token(connect, handler):
    client = connect()
    client.send(b"{}\n")
    assert "token" in client.answer()["error"]
    client.wait_closed()
    assert handler.calls == []


@pytest.mark.parametrize(
    "raw",
    [
        b"hello\n",
        b"\xff\xfe\n",
        b"[]\n",
        b"5\n",
        b"\n",
        b"[" * 100_000 + b"\n",
        b"9" * 9000 + b"\n",
    ],
)
def test_garbage_disconnects(connect, handler, raw):
    client = connect()
    client.send(raw)
    client.wait_closed()
    assert handler.calls == [] and client.received == b""


@pytest.mark.parametrize("end", [b"", b"\n"])
def test_oversize_line(qtbot, server, connect, handler, end):
    client = connect()
    client.send(b"x" * (MAX_LINE + 10) + end)
    client.wait_closed()
    assert handler.calls == []
    qtbot.waitUntil(lambda: server.client_count == 0)


def test_too_many_clients(qtbot, server, connect):
    clients = [connect() for _ in range(MAX_CLIENTS)]
    qtbot.waitUntil(lambda: server.client_count == MAX_CLIENTS)
    extra = connect()
    extra.wait_closed()
    assert all(c.open for c in clients)
    clients[0].socket.abort()
    qtbot.waitUntil(lambda: server.client_count == MAX_CLIENTS - 1)
    assert connect().ask(method="list")["result"] == "ok"


def test_idle_client_is_dropped(qtbot, server, connect, monkeypatch):
    monkeypatch.setattr(server_module, "IDLE_MS", 50)
    client = connect()
    client.wait_closed()
    assert server.client_count == 0


def test_start_and_stop_are_idempotent(qtbot, handler):
    server = ToolServer(handler)
    server.stop()
    assert not server.running and server.name == server.token == ""
    server.start()
    first = server.name, server.token
    server.start()
    assert (server.name, server.token) == first
    client = Client(qtbot, server)
    qtbot.waitUntil(lambda: server.client_count == 1)
    server.stop()
    server.stop()
    client.wait_closed()
    assert server.client_count == 0 and not server.running
    server.start()
    assert server.name != first[0] and server.token != first[1]
    assert len(server.token) >= 40
    server.stop()


def test_stop_during_a_call(qtbot, server, connect, handler):
    client = connect()
    real = handler.__call__

    def stop(method, params):
        server.stop()
        return real(method, params)

    server._handler = stop
    line = json.dumps({"token": client.token, "id": 1, "method": "list"}).encode() + b"\n"
    client.send(line + line)
    client.wait_closed()
    assert len(handler.calls) == 1


def test_socket_is_for_this_user_only(server):
    option = server._server.socketOptions()
    assert option == type(option).UserAccessOption
