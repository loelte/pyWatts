import base64
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from pywatts.sync import base, wahoo
from pywatts.sync.base import (
    MemoryStore,
    NotConnected,
    RateLimited,
    RemoteRide,
    SyncError,
    Transient,
    forget,
)
from pywatts.sync.wahoo import REDIRECT, WahooClient

NOW = 1_800_000_000.0
TODAY = datetime.fromtimestamp(NOW, UTC)
CDN = "https://cdn.example.com/a.fit?sig=LINK"


class Server:
    def __init__(self):
        self.api, self.token, self.files = [], [], []
        self.requests = []
        self.refreshes = 0

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "cdn.example.com":
            return self.files.pop(0)
        assert request.url.host == "api.wahooligan.com"
        if request.url.path == "/oauth/token":
            if self.token:
                return self.token.pop(0)
            self.refreshes += 1
            body = {
                "access_token": f"ACCESS{self.refreshes}",
                "refresh_token": f"REFRESH{self.refreshes}",
                "expires_in": 7200,
                "scope": wahoo.SCOPE,
            }
            return httpx.Response(200, json=body)
        answer = self.api.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def paths(self):
        return [r.url.path for r in self.requests]


@pytest.fixture
def server():
    return Server()


@pytest.fixture
def store():
    s = MemoryStore()
    entry = {"client_id": "ID", "redirect": REDIRECT, "verifier": "VERIFIER"}
    s.set("wahoo", json.dumps({**entry, "refresh_token": "REFRESH0"}))
    return s


class Clock:
    now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make(server, clock):
    def build(store, urls=None, **kw):
        http = httpx.Client(transport=httpx.MockTransport(server))
        opened = [] if urls is None else urls
        return WahooClient(
            store, http, clock=clock, open_browser=lambda u: opened.append(u) or True, **kw
        )

    return build


def workout(number=1, days=1, kind=0, url=None, name="Ride"):
    starts = (TODAY - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    summary = None if url is None else {"file": {"url": url}}
    return {
        "id": number,
        "starts": starts,
        "workout_type_id": kind,
        "name": name,
        "workout_summary": summary,
    }


def listing(*workouts, **headers):
    return httpx.Response(200, json={"workouts": list(workouts)}, headers=headers)


def form(request):
    return {k: v[0] for k, v in parse_qs(request.read().decode()).items()}


# --- login ---


def begun(make, store=None):
    urls = []
    client = make(store or MemoryStore(), urls)
    client.begin_login(" ID ")
    return client, {k: v[0] for k, v in parse_qs(urlsplit(urls[0]).query).items()}


def test_login(make, server):
    store = MemoryStore()
    client, query = begun(make, store)
    assert query["client_id"] == "ID" and query["redirect_uri"] == REDIRECT
    assert query["code_challenge_method"] == "S256" and query["scope"] == "user_read workouts_read"
    assert not client.connected
    client.finish_login(f" {REDIRECT}?code=CODE&state={query['state']} \n")
    assert client.connected
    sent = form(server.requests[0])
    digest = hashlib.sha256(sent["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == query["code_challenge"]
    assert sent["code"] == "CODE" and "client_secret" not in sent
    kept = json.loads(store.get("wahoo"))
    assert kept["refresh_token"] == "REFRESH1" and kept["verifier"] == sent["code_verifier"]
    assert "ACCESS" not in store.get("wahoo")
    with pytest.raises(SyncError):  # a code works once
        client.finish_login(f"{REDIRECT}?code=CODE&state={query['state']}")


@pytest.mark.parametrize(
    "address",
    [
        "",
        "http://[",
        f"{REDIRECT}?code=CODE",
        f"{REDIRECT}?code=CODE&state=wrong",
        f"{REDIRECT}?code=CODE&state=ü",
        "https://evil.example/callback?code=CODE&state={state}",
        "http://localhost:8723/callback?code=CODE&state={state}",
    ],
)
def test_foreign_address(make, server, address):
    client, query = begun(make)
    with pytest.raises(SyncError):
        client.finish_login(address.replace("{state}", query["state"]))
    assert server.requests == [] and not client.connected
    client.finish_login(f"{REDIRECT}?code=CODE&state={query['state']}")  # still possible
    assert client.connected


def test_login_started_in_the_developer_portal(make, server):
    client, _query = begun(make)
    with pytest.raises(SyncError, match="not started by pyWatts"):
        client.finish_login(f"{REDIRECT}?code=CODE")
    assert server.requests == []


def test_login_refused(make, server):
    client, query = begun(make)
    with pytest.raises(SyncError, match="granted"):
        client.finish_login(f"{REDIRECT}?error=access_denied&state={query['state']}")
    client, query = begun(make)
    server.token = [httpx.Response(401)]
    with pytest.raises(NotConnected):
        client.finish_login(f"{REDIRECT}?code=CODE&state={query['state']}")
    client, query = begun(make)
    body = {"access_token": "A", "refresh_token": "R", "scope": "user_read"}
    server.token = [httpx.Response(200, json=body)]
    with pytest.raises(SyncError, match="workouts"):
        client.finish_login(f"{REDIRECT}?code=CODE&state={query['state']}")
    assert not client.connected


def test_scopes_are_separated_as_wahoo_shows_it(make):
    urls = []
    make(MemoryStore(), urls).begin_login("ID")
    assert "scope=user_read%20workouts_read&" in urls[0] and "+" not in urls[0]


def test_login_without_refresh_token(make, server):
    client, query = begun(make)
    server.token = [httpx.Response(200, json={"access_token": "A", "scope": wahoo.SCOPE})]
    with pytest.raises(SyncError, match="refresh token"):
        client.finish_login(f"{REDIRECT}?code=CODE&state={query['state']}")
    assert not client.connected


def test_bad_login_input(make):
    client = make(MemoryStore())
    for args in (("", REDIRECT), ("a b", REDIRECT), ("ID", "http://localhost/x"), ("ID", "x")):
        with pytest.raises(SyncError):
            client.begin_login(*args)
    with pytest.raises(SyncError, match="Connect first"):
        client.finish_login(f"{REDIRECT}?code=CODE")


# --- tokens ---


def test_refresh_rotates_and_needs_the_verifier(make, store, server, clock):
    client = make(store)
    server.api = [listing(), listing(), listing()]
    client.page(1, None)
    client.page(1, None)
    assert server.refreshes == 1
    sent = form(server.requests[0])
    assert sent["code_verifier"] == "VERIFIER" and sent["refresh_token"] == "REFRESH0"
    assert server.requests[1].headers["Authorization"] == "Bearer ACCESS1"
    clock.now += 7200
    client.page(1, None)
    assert json.loads(store.get("wahoo"))["refresh_token"] == "REFRESH2"
    assert json.loads(store.get("wahoo"))["verifier"] == "VERIFIER"


def test_new_refresh_token_is_stored_before_its_first_use(make, store, server):
    server.api = [httpx.ConnectError("x")]
    with pytest.raises(Transient):
        make(store).page(1, None)
    assert json.loads(store.get("wahoo"))["refresh_token"] == "REFRESH1"


def test_denied(make, store, server):
    server.api = [httpx.Response(401), listing()]
    make(store).page(1, None)
    assert server.refreshes == 2
    for answers in ([httpx.Response(401), httpx.Response(401)], [httpx.Response(403)]):
        server.api = answers
        with pytest.raises(NotConnected):
            make(store).page(1, None)
    server.token = [httpx.Response(400, json={})]
    with pytest.raises(NotConnected):
        make(store).page(1, None)
    store.set("wahoo", "junk")
    assert not make(store).connected
    with pytest.raises(NotConnected):
        make(store).page(1, None)


# --- rides ---


def test_page(make, store, server):
    server.api = [
        listing(
            workout(9, days=-3),  # planned
            workout(1, kind=15, url=CDN, name="A"),
            workout(2, kind=1),
            workout(3, kind=42),
            {"id": 4, "starts": "soon"},
            {"id": "x"},
            7,
        )
    ]
    page = make(store).page(1, None)
    assert [(r.external_id, r.cycling) for r in page.rides] == [
        ("1", True),
        ("2", False),
        ("3", None),
    ]
    assert page.rides[0].ref == CDN and page.rides[0].title == "A" and not page.more
    assert page.rides[0].started_at_utc == TODAY - timedelta(days=1)
    assert len(server.requests) == 2  # token and list


def test_more_pages(make, store, server):
    full = [workout(i, days=i) for i in range(1, 31)]
    server.api = [listing(*full), listing(*full), listing(*full[:5])]
    client = make(store)
    assert client.page(1, None).more
    assert not client.page(1, TODAY - timedelta(days=20)).more
    assert not client.page(2, None).more
    assert dict(server.requests[-1].url.params) == {"page": "2", "per_page": "30"}


def test_unexpected_answers(make, store, server):
    client = make(store)
    server.api = [
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json={}),
        httpx.Response(422),
    ]
    for _ in range(3):
        with pytest.raises(SyncError):
            client.page(1, None)


def test_download_without_token(make, store, server):
    client = make(store)
    server.files = [httpx.Response(200, content=b"FIT")]
    assert client.download(RemoteRide("1", TODAY, ref=CDN)) == b"FIT"
    assert len(server.requests) == 1 and "Authorization" not in server.requests[0].headers


def test_download_asks_for_the_link(make, store, server):
    client = make(store)
    server.api = [
        httpx.Response(200, json=workout(5, url=CDN)),
        httpx.Response(200, json=workout(6)),
        httpx.Response(404),
    ]
    server.files = [httpx.Response(200, content=b"FIT")]
    assert client.download(RemoteRide("5", TODAY)) == b"FIT"
    assert client.download(RemoteRide("6", TODAY)) is None
    assert client.download(RemoteRide("7", TODAY)) is None
    assert server.paths[1] == "/v1/workouts/5"


def test_file_redirects(make, store, server):
    client = make(store)
    there = httpx.Response(302, headers={"location": "https://cdn.example.com/b.fit"})
    server.files = [there, httpx.Response(200, content=b"FIT")]
    assert client.download(RemoteRide("1", TODAY, ref=CDN)) == b"FIT"
    server.files = [httpx.Response(302, headers={"location": "http://cdn.example.com/b.fit"})]
    with pytest.raises(SyncError):
        client.download(RemoteRide("1", TODAY, ref=CDN))
    server.files = [there] * 9
    with pytest.raises(SyncError):
        client.download(RemoteRide("1", TODAY, ref=CDN))


@pytest.mark.parametrize(
    "url", ["http://cdn.example.com/a", "file:///c:/a", "https://", "https://["]
)
def test_only_https_links(make, store, server, url):
    with pytest.raises(SyncError):
        make(store).download(RemoteRide("1", TODAY, ref=url))
    assert server.requests == []


def test_file_errors(make, store, server, monkeypatch):
    client = make(store)
    server.files = [httpx.Response(404)]
    assert client.download(RemoteRide("1", TODAY, ref=CDN)) is None
    server.files = [httpx.Response(503)]
    with pytest.raises(Transient):
        client.download(RemoteRide("1", TODAY, ref=CDN))
    monkeypatch.setattr(base, "MAX_FILE_BYTES", 10)
    server.files = [httpx.Response(200, content=b"x" * 11)]
    with pytest.raises(SyncError, match="large"):
        client.download(RemoteRide("1", TODAY, ref=CDN))


# --- limits ---


def test_rate_limit(make, store, server, clock):
    client = make(store)
    server.api = [httpx.Response(429, headers={"X-RateLimit-Reset": "120"})]
    with pytest.raises(RateLimited) as e:
        client.page(1, None)
    assert e.value.retry_after_s == 121
    sent = len(server.requests)
    with pytest.raises(RateLimited):
        client.page(1, None)
    assert len(server.requests) == sent  # nothing is sent while blocked
    clock.now += 122
    server.api = [listing()]
    client.page(1, None)


def test_used_up_quota_blocks_the_next_request(make, store, server):
    client = make(store)
    used = {"X-RateLimit-Remaining": "200, 80, 0", "X-RateLimit-Reset": "30"}
    server.api = [listing(workout(1), **used)]
    assert client.page(1, None).rides
    with pytest.raises(RateLimited) as e:
        client.page(2, None)
    assert e.value.retry_after_s == 31


def test_odd_limit_headers(make, store, server):
    client = make(store)
    server.api = [listing(**{"X-RateLimit-Remaining": "a, b"}), httpx.Response(429)]
    client.page(1, None)
    with pytest.raises(RateLimited) as e:
        client.page(1, None)
    assert e.value.retry_after_s == 301


def test_outages_are_transient(make, store, server):
    client = make(store)
    server.api = [httpx.ConnectError("x"), httpx.ReadTimeout("x"), httpx.Response(503)]
    for _ in range(3):
        with pytest.raises(Transient):
            client.page(1, None)


# --- logout, privacy ---


def test_disconnect(make, store, server):
    client = make(store)
    server.api = [httpx.Response(200)]
    client.disconnect()
    assert server.requests[-1].method == "DELETE" and server.paths[-1] == "/v1/permissions"
    assert store.values == {} and not client.connected
    client.disconnect()


def test_disconnect_offline_or_limited(make, store, server):
    saved = store.get("wahoo")
    for answer in (httpx.ConnectError("x"), httpx.Response(500), httpx.Response(429)):
        store.set("wahoo", saved)
        server.api = [answer]
        make(store).disconnect()
        assert store.values == {}


def test_accounts(make, store, server):
    other = make(store, account="abc")
    assert not other.connected and make(store).connected
    store.set("wahoo-abc", store.get("wahoo"))
    store.set("garmin-abc", "KEY")
    assert other.connected
    forget(store, ("strava", "wahoo", "garmin"), "abc")
    assert list(store.values) == ["wahoo"]
    with pytest.raises(ValueError):
        forget(store, ("wahoo",), "")


def test_nothing_secret_is_logged(make, store, server, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    client, query = begun(make, store)
    client.finish_login(f"{REDIRECT}?code=CODE&state={query['state']}")
    server.api = [listing(workout(1, url=CDN))]
    page = client.page(1, None)
    server.files = [httpx.Response(200, content=b"FIT")]
    client.download(page.rides[0])
    out, err = capfd.readouterr()
    text = caplog.text + out + err + repr(page)
    for secret in ("ACCESS", "REFRESH", "VERIFIER", "CODE", "LINK", query["state"]):
        assert secret not in text
