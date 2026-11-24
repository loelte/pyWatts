import json
import logging
import threading
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from pywatts.sync import oauth
from pywatts.sync.base import MemoryStore, NotConnected, RateLimited, SyncError, Transient
from pywatts.sync.strava import StravaClient, UploadStatus

NOW = 1_800_000_000.0  # a full quarter hour


class Server:
    """Answers with queued responses; token requests are answered unless queued."""

    def __init__(self):
        self.api = []
        self.token = []
        self.requests = []
        self.refreshes = 0

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == "/oauth/token":
            if self.token:
                return self.token.pop(0)
            self.refreshes += 1
            body = {
                "access_token": f"ACCESS{self.refreshes}",
                "refresh_token": f"REFRESH{self.refreshes}",
                "expires_at": NOW + 21600,
            }
            return httpx.Response(200, json=body)
        if request.url.path == "/oauth/revoke":
            return httpx.Response(200)
        answer = self.api.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def server():
    return Server()


@pytest.fixture
def store():
    s = MemoryStore()
    s.set(
        "strava",
        json.dumps({"client_id": "1", "client_secret": "SECRET", "refresh_token": "REFRESH0"}),
    )
    return s


def make(store, server, browser=lambda url: True):
    http = httpx.Client(transport=httpx.MockTransport(server))
    return StravaClient(store, http, clock=lambda: NOW, open_browser=browser)


def ok(**body):
    return httpx.Response(201, json={"id": 5, "id_str": "5", "error": None, **body})


# --- uploads ---


def test_upload_and_poll(store, server):
    server.api = [ok(activity_id=None, status="processing"), ok(activity_id=77)]
    client = make(store, server)
    assert client.upload(b"FITDATA", "My ride", "ext-1") == UploadStatus("5")
    assert client.poll("5") == UploadStatus("5", "77")
    assert json.loads(store.get("strava"))["refresh_token"] == "REFRESH1"
    post = server.requests[1]
    assert post.headers["Authorization"] == "Bearer ACCESS1"
    body = post.read()
    for part in (b"FITDATA", b"My ride", b"ext-1", b"Ride", b'name="trainer"'):
        assert part in body
    assert server.refreshes == 1


@pytest.mark.parametrize(
    "text",
    [
        "pywatts-20250301T120000000000.fit duplicate of activity 21234316",
        "x.fit duplicate of <a href='/activities/21234316' target='_blank'>Ride 9</a>",
    ],
)
@pytest.mark.parametrize("code", [200, 400])
def test_duplicate_counts_as_done(store, server, text, code):
    server.api = [httpx.Response(code, json={"id": 5, "error": text})]
    assert make(store, server).upload(b"", "n", "e").activity_id == "21234316"


def test_errors_are_plain_text(store, server):
    server.api = [httpx.Response(400, json={"id": 5, "error": "<b>bad</b> file"})]
    assert make(store, server).upload(b"", "n", "e") == UploadStatus("5", error="bad file")


def test_unexpected_answers(store, server):
    server.api = [
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json={}),
        httpx.Response(404),
    ]
    client = make(store, server)
    for _ in range(3):
        with pytest.raises(SyncError):
            client.poll("5")


def test_401_refreshes_once(store, server):
    server.api = [httpx.Response(401), ok(activity_id=7)]
    assert make(store, server).poll("5").activity_id == "7"
    assert server.refreshes == 2
    server.api = [httpx.Response(401), httpx.Response(401)]
    with pytest.raises(NotConnected):
        make(store, server).poll("5")


def test_network_and_server_errors_are_transient(store, server):
    server.api = [httpx.ConnectError("x"), httpx.ReadTimeout("x"), httpx.Response(503)]
    client = make(store, server)
    for _ in range(3):
        with pytest.raises(Transient):
            client.poll("5")


@pytest.mark.parametrize(
    ("usage", "wait"), [("200,300", 901), ("200,2000", 86400 - NOW % 86400 + 1)]
)
def test_rate_limit(store, server, usage, wait):
    headers = {"X-RateLimit-Limit": "200,2000", "X-RateLimit-Usage": usage}
    server.api = [httpx.Response(429, headers=headers)]
    with pytest.raises(RateLimited) as e:
        make(store, server).poll("5")
    assert e.value.retry_after_s == wait


def test_revoked_or_missing_login(store, server):
    server.token = [httpx.Response(400, json={})]
    with pytest.raises(NotConnected):
        make(store, server).poll("5")
    store.set("strava", "junk")
    client = make(store, server)
    assert not client.connected
    with pytest.raises(NotConnected):
        client.poll("5")


def test_disconnect(store, server):
    client = make(store, server)
    assert client.connected
    client.disconnect()
    assert not client.connected and store.values == {}
    assert server.requests[-1].url.path == "/oauth/revoke"
    client.disconnect()


# --- login ---


def browser(answers):
    """Plays the browser: calls the redirect URI with each set of parameters."""

    def open_(url):
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}

        def visit():
            for path, extra in answers:
                extra = {"state": query["state"], **extra}
                base = query["redirect_uri"].replace("/callback", path)
                target = f"{base}?" + "&".join(f"{k}={v}" for k, v in extra.items())
                try:
                    urllib.request.urlopen(target, timeout=5).read()
                except urllib.error.HTTPError:
                    pass

        threading.Thread(target=visit, daemon=True).start()
        return True

    return open_


def login(server, answers):
    store = MemoryStore()
    client = make(store, server, browser(answers))
    client.connect(" 12 ", "SECRET", threading.Event())
    return store, client


def test_login(server):
    answers = [
        ("/favicon.ico", {}),
        ("/callback", {"state": "wrong", "code": "EVIL"}),
        ("/callback", {"code": "CODE", "scope": "read,activity:write"}),
    ]
    store, client = login(server, answers)
    assert client.connected and json.loads(store.get("strava"))["client_id"] == "12"
    form = parse_qs(server.requests[0].read().decode())
    assert form["code"] == ["CODE"] and form["client_secret"] == ["SECRET"]


def test_login_refused(server):
    for answer in ({"error": "access_denied"}, {"code": "C", "scope": "read"}):
        with pytest.raises(SyncError):
            login(server, [("/callback", answer)])
    server.token = [httpx.Response(401)]
    with pytest.raises(NotConnected):
        login(server, [("/callback", {"code": "C", "scope": "activity:write"})])
    with pytest.raises(SyncError):
        make(MemoryStore(), server).connect("abc", "x", threading.Event())


def test_login_cancel_timeout_no_browser():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(SyncError, match="cancelled"):
        oauth.browser_login("https://x", {}, cancel, lambda u: True)
    with pytest.raises(SyncError, match="timed out"):
        oauth.browser_login("https://x", {}, threading.Event(), lambda u: True, timeout_s=0.2)
    with pytest.raises(SyncError, match="browser"):
        oauth.browser_login("https://x", {}, threading.Event(), lambda u: False)


def test_nothing_secret_is_logged(store, server, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    server.api = [ok(activity_id=7)]
    make(store, server).upload(b"", "n", "e")
    login(server, [("/callback", {"code": "CODE", "scope": "activity:write"})])
    out, err = capfd.readouterr()
    for secret in ("ACCESS", "REFRESH", "SECRET", "CODE"):
        assert secret not in caplog.text + out + err
