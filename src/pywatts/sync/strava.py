"""Strava: upload only. Their policy forbids keeping data read through the API."""

from __future__ import annotations

import json
import re
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from pywatts.sync.base import (
    NotConnected,
    RateLimited,
    SecretStore,
    SyncError,
    Transient,
    account_key,
)
from pywatts.sync.oauth import browser_login

API = "https://www.strava.com/api/v3"  # https://api-v3.strava.com from 2027-01-04
OAUTH = "https://www.strava.com/oauth"
SCOPE = "activity:write"
_KEY = "strava"  # a JSON entry per account: client_id, client_secret, refresh_token
_EXPIRY_MARGIN_S = 300
_DUPLICATE = re.compile(r"duplicate of\b.*?\bactivit(?:y|ies)\D{0,3}(\d+)", re.I | re.S)


def activity_url(activity_id: str) -> str:
    return f"https://www.strava.com/activities/{activity_id}"


@dataclass(frozen=True)
class UploadStatus:
    """Neither activity_id nor error: still processing."""

    upload_id: str | None
    activity_id: str | None = None
    error: str | None = None


class StravaClient:
    """Blocking; used by one worker thread."""

    def __init__(
        self,
        store: SecretStore,
        http: httpx.Client,
        clock: Callable[[], float] = time.time,
        open_browser: Callable[[str], object] = webbrowser.open,
        account: str = "",
    ) -> None:
        """account: keeps the logins of several riders apart."""
        self._store = store
        self._key = account_key(_KEY, account)
        self._http = http
        self._clock = clock
        self._open_browser = open_browser
        self._access = ""
        self._expires_at = 0.0

    # --- account ---

    @property
    def connected(self) -> bool:
        return self._load() is not None

    def connect(self, client_id: str, client_secret: str, cancel: threading.Event) -> None:
        client_id, client_secret = client_id.strip(), client_secret.strip()
        if not client_id.isdigit() or not client_secret:
            raise SyncError("Enter the client ID (a number) and the client secret.")
        result, redirect = browser_login(
            f"{OAUTH}/authorize",
            {
                "client_id": client_id,
                "response_type": "code",
                "approval_prompt": "force",
                "scope": SCOPE,
            },
            cancel,
            self._open_browser,
        )
        if SCOPE not in re.split(r"[,\s]+", result.get("scope", SCOPE)):
            raise SyncError("Permission to upload was not granted.")
        app = {"client_id": client_id, "client_secret": client_secret}
        answer = self._token(
            {**app, "code": result["code"], "grant_type": "authorization_code"}, redirect
        )
        self._keep(app, answer)

    def disconnect(self) -> None:
        """Forgets the account even if Strava cannot be told."""
        entry = self._load()
        self._access, self._expires_at = "", 0.0
        self._store.delete(self._key)
        if entry is not None:
            try:
                self._http.post(
                    f"{OAUTH}/revoke",
                    auth=(entry["client_id"], entry["client_secret"]),
                    data={"token": entry["refresh_token"]},
                )
            except httpx.HTTPError:
                pass

    def _load(self) -> dict[str, str] | None:
        try:
            entry = json.loads(self._store.get(self._key) or "null")
            ok = all(entry[k] for k in ("client_id", "client_secret", "refresh_token"))
        except (ValueError, TypeError, KeyError):
            return None
        return entry if ok else None

    def _keep(self, app: dict[str, str], answer: dict[str, Any]) -> None:
        try:
            refresh, access = answer["refresh_token"], answer["access_token"]
            expires_at = float(answer["expires_at"])
        except (KeyError, TypeError, ValueError) as e:
            raise SyncError("Unexpected answer from Strava.") from e
        # First: the previous refresh token is already invalid.
        self._store.set(self._key, json.dumps({**app, "refresh_token": refresh}))
        self._access, self._expires_at = access, expires_at

    def _token(self, data: dict[str, str], redirect: str = "") -> dict[str, Any]:
        if redirect:
            data = {**data, "redirect_uri": redirect}
        response = self._send("POST", f"{OAUTH}/token", data=data)
        if response.status_code in (400, 401, 403):
            raise NotConnected("Strava refused the login. Check ID and secret, then reconnect.")
        return _json(response)

    def _refresh(self) -> None:
        entry = self._load()
        if entry is None:
            raise NotConnected("Not connected to Strava.")
        app = {k: entry[k] for k in ("client_id", "client_secret")}
        answer = self._token(
            {**app, "grant_type": "refresh_token", "refresh_token": entry["refresh_token"]}
        )
        self._keep(app, answer)

    # --- uploads ---

    def upload(self, fit: bytes, name: str, external_id: str) -> UploadStatus:
        # Virtual rides and indoor rides differ; trainer=1 marks an indoor one.
        response = self._api(
            "POST",
            "/uploads",
            data={
                "data_type": "fit",
                "sport_type": "Ride",
                "trainer": "1",
                "name": name,
                "external_id": external_id,
            },
            files={"file": (f"{external_id}.fit", fit, "application/octet-stream")},
        )
        return self._status(response)

    def poll(self, upload_id: str) -> UploadStatus:
        return self._status(self._api("GET", f"/uploads/{upload_id}"))

    @staticmethod
    def _status(response: httpx.Response) -> UploadStatus:
        if response.status_code not in (200, 201, 400):
            raise SyncError(f"Strava answered with error {response.status_code}.")
        body = _json(response, check=False)
        upload_id = str(body.get("id_str") or body.get("id") or "") or None
        if body.get("activity_id"):
            return UploadStatus(upload_id, str(body["activity_id"]))
        error = body.get("error") or (
            body.get("status") or body.get("message") if response.status_code == 400 else None
        )
        if error:
            if match := _DUPLICATE.search(str(error)):
                return UploadStatus(upload_id, match[1])
            return UploadStatus(upload_id, error=_plain(str(error)))
        if upload_id is None:
            raise SyncError("Unexpected answer from Strava.")
        return UploadStatus(upload_id)

    # --- HTTP ---

    def _api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._clock() > self._expires_at - _EXPIRY_MARGIN_S:
            self._refresh()
        response = self._authorized(method, path, **kwargs)
        if response.status_code == 401:
            self._refresh()
            response = self._authorized(method, path, **kwargs)
        if response.status_code in (401, 403):
            raise NotConnected("Strava denied access. Reconnect in the settings.")
        return response

    def _authorized(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._access}"}
        return self._send(method, API + path, headers=headers, **kwargs)

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise Transient(f"Strava is not reachable ({type(e).__name__}).") from None
        if response.status_code == 429:
            raise RateLimited("Strava's request limit is reached.", self._limit_wait(response))
        if response.status_code >= 500:
            raise Transient(f"Strava has a problem (error {response.status_code}).")
        return response

    def _limit_wait(self, response: httpx.Response) -> float:
        """Windows end at full quarter hours, the daily one at midnight UTC."""
        period = 900
        for kind in ("X-RateLimit", "X-ReadRateLimit"):
            try:
                limits = response.headers[f"{kind}-Limit"].split(",")
                usage = response.headers[f"{kind}-Usage"].split(",")
                if int(usage[1]) >= int(limits[1]):
                    period = 86400
            except (KeyError, ValueError, IndexError):
                pass
        return period - self._clock() % period + 1


def _json(response: httpx.Response, check: bool = True) -> dict[str, Any]:
    if check and response.status_code != 200:
        raise SyncError(f"Strava answered with error {response.status_code}.")
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise SyncError("Unexpected answer from Strava.")
    return body


def _plain(text: str) -> str:
    return re.sub(r"<[^>]*>", "", text).strip()[:300] or "unknown error"
