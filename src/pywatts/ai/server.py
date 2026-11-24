"""Local socket through which mcp_stdio reaches the running app. GUI thread."""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

from pywatts.ai.tools import ToolError

log = logging.getLogger(__name__)

MAX_LINE = 1_000_000  # bytes
MAX_CLIENTS = 4
MAX_UNSENT = 4_000_000  # bytes; a client that reads nothing is dropped
IDLE_MS = 30_000
GRACE_MS = 500  # for a refused client to read why

_METHODS = ("list", "call")


@dataclass
class _Client:
    idle: QTimer
    buffer: bytearray = field(default_factory=bytearray)
    refused: bool = False


class ToolServer(QObject):
    """One JSON object per line. Request: token, id, method (list | call), name, arguments;
    answer: id and result, or id and error.

    handler(method, {"name", "arguments"}) returns the JSON result; the text of a ToolError
    reaches the client, other exceptions only the log."""

    def __init__(
        self, handler: Callable[[str, dict[str, Any]], object], parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._handler = handler
        self._server: QLocalServer | None = None
        self._clients: dict[QLocalSocket, _Client] = {}
        self._token = ""

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def name(self) -> str:
        """Path of the pipe or socket; "" while stopped."""
        return "" if self._server is None else self._server.fullServerName()

    @property
    def token(self) -> str:
        return self._token

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def start(self) -> None:
        """New name and token each time. OSError if the system refuses."""
        if self._server is not None:
            return
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        server.newConnection.connect(lambda: self._guard(self._accept))
        if not server.listen(f"pywatts-{secrets.token_hex(12)}"):
            text = server.errorString()
            server.deleteLater()
            raise OSError(f"The local tool server cannot start: {text}")
        self._token = secrets.token_urlsafe(32)
        self._server = server

    def stop(self) -> None:
        server, self._server = self._server, None
        self._token = ""
        for socket in list(self._clients):
            self._drop(socket)
        if server is not None:
            server.close()
            server.deleteLater()

    # --- slots ---

    def _accept(self) -> None:
        while self._server is not None:
            socket = self._server.nextPendingConnection()
            if socket is None:
                return
            if len(self._clients) >= MAX_CLIENTS:
                socket.abort()
                socket.deleteLater()
                continue
            idle = QTimer(socket, interval=IDLE_MS, singleShot=True)
            self._clients[socket] = _Client(idle)
            idle.timeout.connect(partial(self._guard, partial(self._drop, socket)))
            socket.disconnected.connect(partial(self._guard, partial(self._drop, socket)))
            socket.readyRead.connect(partial(self._guard, partial(self._read, socket)))
            idle.start()
            self._read(socket)  # what arrived before the signal was connected

    def _read(self, socket: QLocalSocket) -> None:
        client = self._clients.get(socket)
        if client is None:
            return
        data = socket.readAll().data()
        if client.refused or not data:
            return
        client.idle.start()
        client.buffer += data
        while (end := client.buffer.find(b"\n")) >= 0:
            line = bytes(client.buffer[:end])
            del client.buffer[: end + 1]
            if len(line) > MAX_LINE or not self._answer(socket, client, line):
                return self._refuse(socket, client)
            if socket not in self._clients:  # dropped or stopped meanwhile
                return
        if len(client.buffer) > MAX_LINE:
            self._drop(socket)

    def _answer(self, socket: QLocalSocket, client: _Client, line: bytes) -> bool:
        """False: the client is not welcome."""
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):
            return False
        if not isinstance(request, dict):
            return False
        uid = request.get("id")
        if not isinstance(uid, int | str) or len(str(uid)) > 100:
            uid = None
        if not self._authorized(request.get("token")):
            self._send(socket, {"id": uid, "error": "The token is wrong."})
            return False
        method = request.get("method")
        if method not in _METHODS:
            answer: dict[str, Any] = {"error": "unknown method"}
        else:
            answer = self._handle(method, request)
        self._send(socket, {"id": uid} | answer)
        return True

    def _authorized(self, token: object) -> bool:
        if not isinstance(token, str) or not self._token:
            return False
        given = token.encode("utf-8", "replace")
        return hmac.compare_digest(given, self._token.encode())

    def _handle(self, method: str, request: dict[str, Any]) -> dict[str, Any]:
        params = {"name": request.get("name"), "arguments": request.get("arguments")}
        try:
            result = self._handler(method, params)
            json.dumps(result)
            return {"result": result}
        except ToolError as e:
            return {"error": str(e)}
        except Exception:
            log.exception("tool handler failed")
            return {"error": "internal error"}

    def _send(self, socket: QLocalSocket, answer: dict[str, Any]) -> None:
        if socket not in self._clients:
            return
        socket.write(json.dumps(answer).encode() + b"\n")
        socket.flush()
        if socket.bytesToWrite() > MAX_UNSENT:
            self._drop(socket)

    def _refuse(self, socket: QLocalSocket, client: _Client) -> None:
        """Closing at once could discard an answer that is not read yet."""
        if socket in self._clients:
            client.refused = True
            client.buffer.clear()
            client.idle.start(GRACE_MS)

    def _drop(self, socket: QLocalSocket) -> None:
        client = self._clients.pop(socket, None)
        if client is None:
            return
        client.idle.stop()
        socket.abort()
        socket.deleteLater()

    @staticmethod
    def _guard(action: Callable[[], None]) -> None:
        """Never let a failure escape a slot: PyQt6 would abort the app."""
        try:
            action()
        except Exception:
            log.exception("tool server failure")
