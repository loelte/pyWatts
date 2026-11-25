"""Garmin Personal API: rides only, read only, with a key the rider makes in Garmin Connect.

Experimental: paths and login are from Garmin's own Python client, the fields of the activity
list are documented nowhere, so nothing but the id is required. See scripts/garmin_spike.py.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

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
    read_limited,
)

NAME = "garmin"
KEY_PAGE = "https://connect.garmin.com/app/settings/personalApiToken"
API = "https://personal.connectapi.garmin.com"
TOKEN = "/personalapitoken-service/personal-api/personal-api-key/token"
SEARCH = "/activitylist-service/activities/search/activities"
FILE = "/download-service/files/activity"
PER_PAGE = 20  # default of Garmin's client; the most that is allowed is not documented
_EXPIRY_MARGIN_S = 300
_LIMIT_S = 900
_CYCLING = re.compile(r"cycl|bik|ride|bmx")
_OTHER = re.compile(r"run|walk|hik|swim|strength|yoga|pilates|row|ski|golf|tennis|climb|cardio")


class GarminClient:
    """Blocking; used by one worker thread."""

    def __init__(
        self,
        store: SecretStore,
        http: httpx.Client,
        clock: Callable[[], float] = time.time,
        account: str = "",
    ) -> None:
        self._store = store
        self._key = account_key(NAME, account)
        self._http = http
        self._clock = clock
        self._access = ""
        self._expires_at = 0.0

    # --- account ---

    @property
    def connected(self) -> bool:
        return bool(self._store.get(self._key))

    def connect(self, key: str) -> None:
        key = key.strip()
        if not re.fullmatch(r"[\x21-\x7e]+", key):
            raise SyncError("Enter the API key from Garmin Connect.")
        access, expires_at = self._exchange(key)  # is it valid?
        self._store.set(self._key, key)
        self._access, self._expires_at = access, expires_at

    def disconnect(self) -> None:
        """The key stays valid until it is deleted in Garmin Connect."""
        self._access, self._expires_at = "", 0.0
        self._store.delete(self._key)

    def _exchange(self, key: str) -> tuple[str, float]:
        headers = {"Accept": "*/*", "Authorization": f"ApiKey {key}"}
        response = self._send("POST", TOKEN, headers=headers)
        if response.status_code in (400, 401, 403):
            raise NotConnected("Garmin refused the key. Create a new one in Garmin Connect.")
        if response.status_code not in (200, 201):
            raise SyncError(f"Garmin answered with error {response.status_code}.")
        access = response.text.strip().strip('"')
        try:
            payload = access.split(".")[1]
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            return access, float(data["exp"])
        except (IndexError, ValueError, TypeError, KeyError):
            raise SyncError("Unexpected answer from Garmin.") from None

    def _refresh(self) -> None:
        key = self._store.get(self._key)
        if not key:
            raise NotConnected("Not connected to Garmin.")
        self._access, self._expires_at = self._exchange(key)

    # --- rides ---

    def page(self, number: int, since: datetime | None) -> Page:
        params: dict[str, str | int] = {"start": (number - 1) * PER_PAGE, "limit": PER_PAGE}
        if since is not None:  # a day more: dates are probably local ones
            today = datetime.fromtimestamp(self._clock(), UTC).date()
            params["startDate"] = (since.date() - timedelta(days=1)).isoformat()
            params["endDate"] = (today + timedelta(days=1)).isoformat()
        response = self._request(SEARCH, {"Accept": "application/json"}, params)
        if response.status_code != 200:
            raise SyncError(f"Garmin answered with error {response.status_code}.")
        try:
            found = response.json()
        except ValueError:
            found = None
        if isinstance(found, dict):  # wrapped
            found = next((v for v in found.values() if isinstance(v, list)), None)
        if not isinstance(found, list):
            raise SyncError("Unexpected answer from Garmin.")
        rides = [r for a in found if isinstance(a, dict) and (r := _ride(a)) is not None]
        if found and not rides:
            raise SyncError("Unexpected answer from Garmin.")
        return Page(rides, len(found) >= PER_PAGE)

    def download(self, ride: RemoteRide) -> bytes | None:
        accept = {
            "Accept": "application/zip, application/x-zip-compressed, application/octet-stream"
        }
        response = self._request(f"{FILE}/{ride.external_id}", accept)
        return read_limited(response) or None

    # --- HTTP ---

    def _request(
        self, path: str, headers: dict[str, str], params: dict[str, str | int] | None = None
    ) -> httpx.Response:
        if self._clock() > self._expires_at - _EXPIRY_MARGIN_S:
            self._refresh()
        response = self._authorized(path, headers, params)
        if response.status_code == 401:
            self._refresh()
            response = self._authorized(path, headers, params)
        if response.status_code == 401:
            raise NotConnected("Garmin denied access. Create a new key in Garmin Connect.")
        return response

    def _authorized(
        self, path: str, headers: dict[str, str], params: dict[str, str | int] | None
    ) -> httpx.Response:
        login = {"Authorization": f"Bearer {self._access}"}
        return self._send("GET", path, headers={**headers, **login}, params=params)

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, API + path, **kwargs)
        except httpx.HTTPError as e:
            raise Transient(f"Garmin is not reachable ({type(e).__name__}).") from None
        if response.status_code == 429:
            try:
                wait = min(max(float(response.headers["Retry-After"]), 1.0), 86400.0)
            except (KeyError, ValueError):
                wait = _LIMIT_S
            raise RateLimited(
                "Garmin's request limit is reached. It goes on later by itself.", wait
            )
        if response.status_code >= 500:
            raise Transient(f"Garmin has a problem (error {response.status_code}).")
        return response


def _ride(activity: dict[str, Any]) -> RemoteRide | None:
    number = str(activity.get("activityId", ""))
    if not (number.isascii() and number.isdigit()):
        return None
    try:
        started = datetime.fromisoformat(str(activity.get("startTimeGMT"))).replace(tzinfo=UTC)
    except ValueError:
        started = None
    kind = activity.get("activityType")
    if isinstance(kind, dict):
        kind = kind.get("typeKey")
    text = kind.lower() if isinstance(kind, str) else ""
    cycling = True if _CYCLING.search(text) else False if _OTHER.search(text) else None
    title = activity.get("activityName")
    return RemoteRide(number, started, title if isinstance(title, str) else "", cycling)
