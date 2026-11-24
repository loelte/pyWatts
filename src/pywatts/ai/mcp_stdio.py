"""MCP server over stdio, started by the AI CLI: python -m pywatts.ai.mcp_stdio

A relay: every tool runs in the app, reached through its local socket. No Qt and no database here.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from collections.abc import Callable
from typing import Any, BinaryIO

from pywatts import __version__

PIPE_ENV = "PYWATTS_COACH_PIPE"
TOKEN_ENV = "PYWATTS_COACH_TOKEN"
PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")  # oldest first
MAX_LINE = 1_000_000  # bytes; as in server.py
BUSY_RETRY_S = 2.0

PARSE_ERROR, INVALID_REQUEST, NO_METHOD, BAD_PARAMS = -32700, -32600, -32601, -32602
APP_ERROR = -32000

Ask = Callable[..., Any]


class AppGone(Exception):
    def __init__(self) -> None:
        super().__init__("pyWatts is not running")


class AppError(Exception):
    """The app answered with this text."""


class _RpcError(Exception):
    def __init__(self, code: int, text: str) -> None:
        super().__init__(text)
        self.code = code


# --- the app ---


def _connect(path: str) -> BinaryIO:
    if sys.platform != "win32":
        import socket

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(path)
            return sock.makefile("rwb", buffering=0)  # keeps the connection open
    deadline = time.monotonic() + BUSY_RETRY_S
    while True:
        try:
            return open(path, "r+b", buffering=0)
        except FileNotFoundError:
            raise
        except OSError:  # all instances of the pipe are busy
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _exchange(path: str, line: bytes) -> bytes:
    with _connect(path) as stream:
        while line:
            line = line[stream.write(line) :]
        answer = bytearray()
        while not answer.endswith(b"\n"):
            part = stream.read(65536)
            if not part:
                raise ConnectionError
            answer += part
    return bytes(answer)


def ask_app(method: str, **fields: Any) -> Any:
    """One request over its own connection, so that a restarted app just works."""
    path, token = os.environ.get(PIPE_ENV, ""), os.environ.get(TOKEN_ENV, "")
    if not path:
        raise AppGone
    request = {"token": token, "id": 1, "method": method} | fields
    line = json.dumps(request).encode() + b"\n"
    if len(line) > MAX_LINE:
        raise AppError("The request is too large: send less at once.")
    try:
        answer = json.loads(_exchange(path, line))
    except (OSError, ValueError) as e:
        raise AppGone from e
    if not isinstance(answer, dict) or "error" in answer or "result" not in answer:
        error = answer.get("error") if isinstance(answer, dict) else None
        raise AppError(str(error or "pyWatts gave no valid answer"))
    return answer["result"]


# --- MCP ---


def _initialize(params: dict[str, Any], _ask: Ask) -> Any:
    wanted = params.get("protocolVersion")
    return {
        "protocolVersion": wanted if wanted in PROTOCOLS else PROTOCOLS[-1],
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "pywatts", "version": __version__},
    }


def _ping(_params: dict[str, Any], _ask: Ask) -> Any:
    return {}


def _list(_params: dict[str, Any], ask: Ask) -> Any:
    try:
        return {"tools": ask("list")}
    except (AppGone, AppError) as e:
        raise _RpcError(APP_ERROR, str(e)) from e


def _call(params: dict[str, Any], ask: Ask) -> Any:
    name, arguments = params.get("name"), params.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, dict | None):
        raise _RpcError(BAD_PARAMS, "name must be text and arguments an object")
    try:
        result, failed = ask("call", name=name, arguments=arguments or {}), False
    except (AppGone, AppError) as e:
        result, failed = str(e), True
    text = result if isinstance(result, str) else json.dumps(result)
    return {"content": [{"type": "text", "text": text}], "isError": failed}


_HANDLERS = {
    "initialize": _initialize,
    "ping": _ping,
    "tools/list": _list,
    "tools/call": _call,
}


def _error(uid: Any, code: int, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": uid, "error": {"code": code, "message": text}}


def _handle_one(message: Any, ask: Ask) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "a message must be an object")
    if "method" not in message and ("result" in message or "error" in message):
        return None  # an answer, though nothing was asked
    if "id" not in message and "method" in message:
        return None  # a notification
    uid, method, params = message.get("id"), message.get("method"), message.get("params")
    if isinstance(uid, bool) or not isinstance(uid, int | str):
        return _error(None, INVALID_REQUEST, "id must be a whole number or text")
    if not isinstance(method, str):
        return _error(uid, INVALID_REQUEST, "method must be text")
    if not isinstance(params, dict | None):
        return _error(uid, BAD_PARAMS, "params must be an object")
    handler = _HANDLERS.get(method)
    if handler is None:
        return _error(uid, NO_METHOD, "unknown method")
    try:
        return {"jsonrpc": "2.0", "id": uid, "result": handler(params or {}, ask)}
    except _RpcError as e:
        return _error(uid, e.code, str(e))


def handle_line(line: bytes, ask: Ask = ask_app) -> bytes | None:
    """The answer to one line of input, None if there is none."""
    try:
        message = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):
        answer: Any = _error(None, PARSE_ERROR, "invalid JSON")
    else:
        if not isinstance(message, list):
            answer = _handle_one(message, ask)
        elif message:
            answers = (_handle_one(m, ask) for m in message)
            answer = [a for a in answers if a is not None] or None
        else:
            answer = _error(None, INVALID_REQUEST, "an empty batch")
    return None if answer is None else json.dumps(answer).encode() + b"\n"


def serve(source: BinaryIO, sink: BinaryIO, ask: Ask = ask_app) -> None:
    """Until the input ends."""
    while line := source.readline(MAX_LINE):
        if len(line) == MAX_LINE and not line.endswith(b"\n"):
            while (rest := source.readline(MAX_LINE)) and not rest.endswith(b"\n"):
                pass
            line = b"oversized"  # answered as invalid JSON
        if not line.strip():
            continue
        answer = handle_line(line, ask)
        if answer is not None:
            sink.write(answer)
            sink.flush()


def main() -> None:
    # Ctrl+C in the terminal is meant for the CLI, which closes the input when it ends.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sink = sys.stdout.buffer
    sys.stdout = sys.stderr  # nothing else may reach the protocol
    try:
        serve(sys.stdin.buffer, sink)
    except OSError:
        pass  # the CLI is gone
    finally:
        sys.stdout = sys.__stdout__


if __name__ == "__main__":
    main()
