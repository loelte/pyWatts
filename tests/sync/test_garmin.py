import base64
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from pywatts.sync.base import (
    MemoryStore,
    NotConnected,
    RateLimited,
    RemoteRide,
    SyncError,
    Transient,
)
from pywatts.sync.garmin import FILE, SEARCH, TOKEN, GarminClient

NOW = 1_800_000_000.0
TODAY = datetime.fromtimestamp(NOW, UTC)


def jwt(exp=NOW + 86400, name="ACCESS"):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp, "n": name}).encode())
    return f"{name}.{payload.decode().rstrip('=')}.s"


class Server:
    def __init__(self):
        self.api, self.token, self.requests = [], [], []

    def __call__(self, request):
        assert request.url.host == "personal.connectapi.garmin.com"
        self.requests.append(request)
        if request.url.path == TOKEN:
            return self.token.pop(0) if self.token else httpx.Response(200, text=f"{jwt()}\n")
        answer = self.api.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def exchanges(self):
        return sum(r.url.path == TOKEN for r in self.requests)


@pytest.fixture
def server():
    return Server()


@pytest.fixture
def store():
    s = MemoryStore()
    s.set("garmin", "KEY")
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
    def build(store, **kw):
        http = httpx.Client(transport=httpx.MockTransport(server))
        return GarminClient(store, http, clock=clock, **kw)

    return build


def activity(number=1, kind="road_biking", at="2026-11-20 08:00:12", name="Morning"):
    return {
        "activityId": number,
        "activityName": name,
        "startTimeGMT": at,
        "activityType": {"typeKey": kind},
    }


def test_connect(make, server):
    store = MemoryStore()
    client = make(store)
    client.connect(" KEY\n")
    assert client.connected and store.get("garmin") == "KEY"
    request = server.requests[0]
    assert request.method == "POST" and request.headers["Authorization"] == "ApiKey KEY"
    server.api = [httpx.Response(200, json=[])]
    client.page(1, None)
    assert server.exchanges == 1
    assert server.requests[-1].headers["Authorization"] == f"Bearer {jwt()}"


def test_bad_key(make, server):
    store = MemoryStore()
    client = make(store)
    for key in ("", "  ", "a b", "ü"):
        with pytest.raises(SyncError):
            client.connect(key)
    assert server.requests == []
    for answer in (httpx.Response(401), httpx.Response(403)):
        server.token = [answer]
        with pytest.raises(NotConnected):
            client.connect("KEY")
    for text in ("", "a.b.c", "nothing", "a.e30.c"):
        server.token = [httpx.Response(200, text=text)]
        with pytest.raises(SyncError):
            client.connect("KEY")
    assert store.values == {}


def test_token_is_renewed(make, store, server, clock):
    client = make(store)
    server.api = [httpx.Response(200, json=[]) for _ in range(3)]
    client.page(1, None)
    client.page(1, None)
    assert server.exchanges == 1
    clock.now += 86400
    server.token = [httpx.Response(200, text=jwt(clock.now + 86400))]
    client.page(1, None)
    assert server.exchanges == 2


def test_denied(make, store, server):
    server.api = [httpx.Response(401), httpx.Response(200, json=[])]
    make(store).page(1, None)
    server.api = [httpx.Response(401), httpx.Response(401)]
    with pytest.raises(NotConnected):
        make(store).page(1, None)
    server.token = [httpx.Response(401)]  # expired
    with pytest.raises(NotConnected):
        make(store).page(1, None)
    with pytest.raises(NotConnected):
        make(MemoryStore()).page(1, None)


def test_page(make, store, server):
    server.api = [
        httpx.Response(
            200,
            json=[
                activity(1),
                activity(2, "running", "2026-11-19T07:00:00.0"),
                activity(3, "multi_sport", "junk"),
                {"activityId": 4},
                {"activityName": "no id"},
                "x",
            ],
        )
    ]
    since = datetime(2026, 11, 1, 5, tzinfo=UTC)
    page = make(store).page(1, since)
    assert [(r.external_id, r.cycling) for r in page.rides] == [
        ("1", True),
        ("2", False),
        ("3", None),
        ("4", None),
    ]
    assert page.rides[0].started_at_utc == datetime(2026, 11, 20, 8, 0, 12, tzinfo=UTC)
    assert page.rides[1].started_at_utc == datetime(2026, 11, 19, 7, tzinfo=UTC)
    assert page.rides[2].started_at_utc is None and not page.more
    query = dict(server.requests[-1].url.params)
    tomorrow = (TODAY.date() + timedelta(days=1)).isoformat()
    assert query == {"start": "0", "limit": "20", "startDate": "2026-10-31", "endDate": tomorrow}


def test_more_pages_and_wrapped_lists(make, store, server):
    rows = [activity(i) for i in range(1, 21)]
    server.api = [httpx.Response(200, json=rows), httpx.Response(200, json={"a": 1, "list": rows})]
    client = make(store)
    assert client.page(1, None).more
    assert len(client.page(3, None).rides) == 20
    assert dict(server.requests[-1].url.params) == {"start": "40", "limit": "20"}
    assert server.requests[-1].url.path == SEARCH


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json={}),
        httpx.Response(200, json=7),
        httpx.Response(200, json=[{"id": 1}]),  # other names: say so instead of finding nothing
        httpx.Response(404),
    ],
)
def test_unexpected_answers(make, store, server, answer):
    server.api = [answer]
    with pytest.raises(SyncError):
        make(store).page(1, None)


def test_download(make, store, server):
    client = make(store)
    server.api = [httpx.Response(200, content=b"PKzip"), httpx.Response(404), httpx.Response(200)]
    ride = RemoteRide("12", TODAY)
    assert client.download(ride) == b"PKzip"
    assert server.requests[-1].url.path == f"{FILE}/12"
    assert client.download(ride) is None
    assert client.download(ride) is None  # empty


def test_outages_and_limits(make, store, server):
    client = make(store)
    server.api = [httpx.ConnectError("x"), httpx.Response(500)]
    for _ in range(2):
        with pytest.raises(Transient):
            client.page(1, None)
    for headers, wait in (({"Retry-After": "60"}, 60), ({"Retry-After": "soon"}, 900), ({}, 900)):
        server.api = [httpx.Response(429, headers=headers)]
        with pytest.raises(RateLimited) as e:
            client.page(1, None)
        assert e.value.retry_after_s == wait


def test_accounts_and_disconnect(make, store, server):
    other = make(store, account="abc")
    assert not other.connected
    other.connect("OTHER")
    make(store).disconnect()
    assert store.values == {"garmin-abc": "OTHER"}
    make(store).disconnect()


def test_nothing_secret_is_logged(make, server, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    client = make(MemoryStore())
    client.connect("KEY")
    server.api = [httpx.Response(200, json=[activity()])]
    client.page(1, None)
    server.token = [httpx.Response(401)]
    with pytest.raises(NotConnected) as e:
        client.connect("KEY")
    out, err = capfd.readouterr()
    for secret in ("KEY", "ACCESS"):
        assert secret not in caplog.text + out + err + str(e.value)
