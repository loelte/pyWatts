"""Browser login with a one-shot listener on the loopback interface."""

from __future__ import annotations

import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from pywatts.sync.base import SyncError

LOGIN_TIMEOUT_S = 180.0
_HOST = "127.0.0.1"


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows it would let others share the port
    result: dict[str, str] | None = None
    state = ""


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    timeout = 5  # browsers open idle connections

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        valid = url.path == "/callback" and secrets.compare_digest(
            query.get("state", "").encode(), self.server.state.encode()
        )
        # Anything else is ignored, so that other pages cannot end the login.
        if valid and self.server.result is None:
            self.server.result = query
        text = b"Done. You can close this page and return to pyWatts." if valid else b"Not found"
        self.send_response(200 if valid else 404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(text)))
        self.end_headers()
        self.wfile.write(text)

    def log_message(self, format: str, *args: object) -> None:
        pass  # the URL contains the login code


def browser_login(
    authorize_url: str,
    params: dict[str, str],
    cancel: threading.Event,
    open_browser: Callable[[str], object] = webbrowser.open,
    timeout_s: float = LOGIN_TIMEOUT_S,
) -> tuple[dict[str, str], str]:
    """Returns the callback's query and the redirect URI. Raises SyncError."""
    with _Server((_HOST, 0), _Handler) as server:
        server.state = secrets.token_urlsafe(32)
        redirect = f"http://{_HOST}:{server.server_address[1]}/callback"
        query = {**params, "redirect_uri": redirect, "state": server.state}
        thread = threading.Thread(target=server.serve_forever, args=(0.1,), daemon=True)
        thread.start()
        try:
            if not open_browser(f"{authorize_url}?{urlencode(query)}"):
                raise SyncError("Could not open the browser.")
            deadline = time.monotonic() + timeout_s
            while server.result is None:
                if cancel.wait(0.1):
                    raise SyncError("Login cancelled.")
                if time.monotonic() > deadline:
                    raise SyncError("Login timed out.")
        finally:
            server.shutdown()
    result = server.result
    if "code" not in result:
        raise SyncError("Access was not granted.")
    return result, redirect
