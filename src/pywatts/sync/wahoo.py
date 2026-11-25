"""Wahoo: rides only, read only. Public client (PKCE), so there is no secret."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

from pywatts.sync.base import (
    NotConnected,
    Page,
    RateLimited,
    RemoteRide,
    SecretStore,
    SyncError,
    Transient,
    account_key,
    fetch_file,
)

NAME = "wahoo"
API = "https://api.wahooligan.com"
REDIRECT = "https://localhost:8723/callback"  # nothing listens there: the address is pasted
SCOPE = "user_read workouts_read"  # each must be switched on for the app, else invalid_scope
PER_PAGE = 30
_EXPIRY_MARGIN_S = 120
_TOKEN_S = 7200
_LIMIT_S = 300  # shortest window of the request limits
_ENTRY = ("client_id", "redirect", "verifier", "refresh_token")
_CYCLING = {0, 11, 12, 13, 14, 15, 16, 17, 49, 61, 64, 68, 70}
# Running, walking, swimming, snow, skating, water, yoga. Any other type, e.g. a general
# workout or a fitness machine: the file tells.
_OTHER = {1, *range(3, 11), *range(25, 42), 46, 56, 66, 67, 71}


@dataclass(frozen=True)
class _Login:
    client_id: str
    redirect: str
    state: str
    verifier: str


class WahooClient:
    """Blocking; used by one worker thread."""

    def __init__(
        self,
        store: SecretStore,
        http: httpx.Client,
        clock: Callable[[], float] = time.time,
        open_browser: Callable[[str], object] = webbrowser.open,
        account: str = "",
    ) -> None:
        self._store = store
        self._key = account_key(NAME, account)
        self._http = http
        self._clock = clock
        self._open_browser = open_browser
        self._login: _Login | None = None
        self._access = ""
        self._expires_at = 0.0
        self._blocked_until = 0.0

    # --- account ---

    @property
    def connected(self) -> bool:
        return self._load() is not None

    def begin_login(self, client_id: str, redirect: str = REDIRECT) -> None:
        """Opens the browser. The rider pastes the address it ends at into finish_login."""
        self._login = None
        client_id, redirect = client_id.strip(), redirect.strip()
        if not re.fullmatch(r"[\x21-\x7e]+", client_id):
            raise SyncError("Enter the client ID of your Wahoo app.")
        parts = urlsplit(redirect)
        if parts.scheme != "https" or not parts.hostname:
            raise SyncError("The redirect address must start with https://.")
        login = _Login(client_id, redirect, secrets.token_urlsafe(32), secrets.token_urlsafe(64))
        digest = hashlib.sha256(login.verifier.encode()).digest()
        query = {
            "client_id": client_id,
            "redirect_uri": redirect,
            "scope": SCOPE,
            "response_type": "code",
            "state": login.state,
            "code_challenge": base64.urlsafe_b64encode(digest).decode().rstrip("="),
            "code_challenge_method": "S256",
        }
        # Spaces as %20, as in Wahoo's examples.
        if not self._open_browser(f"{API}/oauth/authorize?{urlencode(query, quote_via=quote)}"):
            raise SyncError("Could not open the browser.")
        self._login = login

    def finish_login(self, address: str) -> None:
        login = self._login
        if login is None:
            raise SyncError("Press Connect first.")
        try:
            got, want = urlsplit(address.strip()), urlsplit(login.redirect)
            query = {k: v[0] for k, v in parse_qs(got.query).items()}
        except ValueError:
            raise SyncError("This is not an address.") from None
        same = (got.scheme, got.netloc, got.path) == (want.scheme, want.netloc, want.path)
        if same and "code" in query and "state" not in query:  # e.g. from the developer portal
            raise SyncError(
                "This login was not started by pyWatts. Cancel, press Connect and use the address "
                "that the page opened by it ends at (with “state=” in it)."
            )
        state = query.get("state", "").encode()
        if not same or not secrets.compare_digest(state, login.state.encode()):
            raise SyncError("The address does not belong to this login. Copy it completely.")
        self._login = None  # a code works once
        if not query.get("code"):
            raise SyncError("Access was not granted.")
        answer = self._token(
            client_id=login.client_id,
            code_verifier=login.verifier,
            grant_type="authorization_code",
            code=query["code"],
            redirect_uri=login.redirect,
        )
        granted = answer.get("scope")
        if isinstance(granted, str) and "workouts_read" not in granted.split():
            raise SyncError("Permission to read workouts was not granted.")
        self._keep(
            {
                "client_id": login.client_id,
                "redirect": login.redirect,
                "verifier": login.verifier,
            },
            answer,
        )

    def disconnect(self) -> None:
        """Forgets the account even if Wahoo cannot be told."""
        self._login, self._blocked_until = None, 0.0
        try:
            if self._load() is not None:
                self._request("DELETE", "/v1/permissions")
        except SyncError:
            pass
        finally:
            self._access, self._expires_at = "", 0.0
            self._store.delete(self._key)

    def _load(self) -> dict[str, str] | None:
        try:
            entry = json.loads(self._store.get(self._key) or "null")
            ok = all(isinstance(entry[k], str) and entry[k] for k in _ENTRY)
        except (ValueError, TypeError, KeyError):
            return None
        return entry if ok else None

    def _keep(self, app: dict[str, str], answer: dict[str, Any]) -> None:
        refresh, access = answer.get("refresh_token"), answer.get("access_token")
        if not (isinstance(access, str) and access):
            raise SyncError("Unexpected answer from Wahoo.")
        if not (isinstance(refresh, str) and refresh):
            raise SyncError("Wahoo granted access only for two hours (no refresh token).")
        lifetime = answer.get("expires_in")
        seconds = lifetime if isinstance(lifetime, int | float) and lifetime > 0 else _TOKEN_S
        # First: using the new access token makes the previous refresh token invalid.
        self._store.set(self._key, json.dumps({**app, "refresh_token": refresh}))
        self._access, self._expires_at = access, self._clock() + seconds

    def _token(self, **data: str) -> dict[str, Any]:
        response = self._send("POST", f"{API}/oauth/token", data=data)
        if response.status_code in (400, 401, 403):
            raise NotConnected("Wahoo refused the login. Please connect again.")
        return _json(response)

    def _refresh(self) -> None:
        entry = self._load()
        if entry is None:
            raise NotConnected("Not connected to Wahoo.")
        answer = self._token(
            client_id=entry["client_id"],
            code_verifier=entry["verifier"],
            grant_type="refresh_token",
            refresh_token=entry["refresh_token"],
        )
        self._keep({k: entry[k] for k in ("client_id", "redirect", "verifier")}, answer)

    # --- rides ---

    def page(self, number: int, since: datetime | None) -> Page:
        """Newest first. Workouts without a start in the past are left out."""
        body = _json(self._request("GET", f"/v1/workouts?page={number}&per_page={PER_PAGE}"))
        workouts = body.get("workouts")
        if not isinstance(workouts, list):
            raise SyncError("Unexpected answer from Wahoo.")
        rides = [r for w in workouts if isinstance(w, dict) and (r := _ride(w)) is not None]
        now = datetime.fromtimestamp(self._clock(), UTC)
        more = len(workouts) >= PER_PAGE
        if since is not None and any(r.started_at_utc < since for r in rides if r.started_at_utc):
            more = False
        return Page([r for r in rides if r.started_at_utc and r.started_at_utc <= now], more)

    def download(self, ride: RemoteRide) -> bytes | None:
        url = ride.ref
        if not url:
            response = self._request("GET", f"/v1/workouts/{ride.external_id}")
            if response.status_code == 404:
                return None
            url = _file_url(_json(response))
        return fetch_file(self._http, url) if url else None

    # --- HTTP ---

    def _request(self, method: str, path: str) -> httpx.Response:
        if self._clock() < self._blocked_until:
            raise self._limited()
        if self._clock() > self._expires_at - _EXPIRY_MARGIN_S:
            self._refresh()
        response = self._authorized(method, path)
        if response.status_code == 401:
            self._refresh()
            response = self._authorized(method, path)
        if response.status_code in (401, 403):
            raise NotConnected("Wahoo denied access. Please connect again.")
        return response

    def _authorized(self, method: str, path: str) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._access}"}
        response = self._send(method, API + path, headers=headers)
        if 0 in _numbers(response, "X-RateLimit-Remaining"):
            self._block(response)  # this answer is still good, the next request is not
        return response

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise Transient(f"Wahoo is not reachable ({type(e).__name__}).") from None
        if response.status_code == 429:
            self._block(response)
            raise self._limited()
        if response.status_code >= 500:
            raise Transient(f"Wahoo has a problem (error {response.status_code}).")
        return response

    def _block(self, response: httpx.Response) -> None:
        reset = _numbers(response, "X-RateLimit-Reset")
        wait = max(reset) if reset and max(reset) > 0 else _LIMIT_S
        self._blocked_until = self._clock() + min(wait, 86400) + 1

    def _limited(self) -> RateLimited:
        wait = max(0.0, self._blocked_until - self._clock())
        return RateLimited("Wahoo's request limit is reached. It goes on later by itself.", wait)


def _ride(workout: dict[str, Any]) -> RemoteRide | None:
    number, kind = workout.get("id"), workout.get("workout_type_id")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return None
    try:
        starts = datetime.fromisoformat(str(workout.get("starts")))
    except ValueError:
        return None
    if starts.tzinfo is None:
        starts = starts.replace(tzinfo=UTC)
    cycling = True if kind in _CYCLING else False if kind in _OTHER else None
    title = workout.get("name")
    return RemoteRide(
        str(number),
        starts.astimezone(UTC),
        title if isinstance(title, str) else "",
        cycling,
        _file_url(workout),
    )


def _file_url(workout: dict[str, Any]) -> str:
    """ "" if there is none, e.g. of a workout that was only planned."""
    summary = workout.get("workout_summary")
    file = summary.get("file") if isinstance(summary, dict) else None
    url = file.get("url") if isinstance(file, dict) else None
    return url if isinstance(url, str) else ""


def _numbers(response: httpx.Response, header: str) -> list[int]:
    try:
        return [int(n) for n in response.headers.get(header, "").split(",")]
    except ValueError:
        return []


def _json(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise SyncError(f"Wahoo answered with error {response.status_code}.")
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise SyncError("Unexpected answer from Wahoo.")
    return body
