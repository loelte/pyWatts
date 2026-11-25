"""Shared by all services: errors, HTTP client, secret storage."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx
import keyring
import keyring.errors

from pywatts.storage.import_files import MAX_FILE_BYTES

_KEYRING_SERVICE = "pywatts"
TIMEOUT_S = 15.0
_REDIRECTS = 3

# They log URLs and, at debug level, headers.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class SyncError(Exception):
    """Final; the text is shown to the user."""


class NotConnected(SyncError):
    """The user has to connect (again)."""


class Transient(SyncError):
    """Worth repeating later."""


class RateLimited(Transient):
    def __init__(self, text: str, retry_after_s: float) -> None:
        super().__init__(text)
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class RemoteRide:
    """An activity in the list of a service."""

    external_id: str
    started_at_utc: datetime | None  # None: unknown
    title: str = ""
    cycling: bool | None = None  # None: the file tells
    ref: str = field(default="", repr=False)  # of the service, e.g. a link


@dataclass(frozen=True)
class Page:
    rides: list[RemoteRide]
    more: bool


class ActivitySource(Protocol):
    """A service with rides. Blocking; used by one worker thread."""

    @property
    def connected(self) -> bool: ...

    def disconnect(self) -> None: ...

    def page(self, number: int, since: datetime | None) -> Page:
        """From 1. It may contain rides older than `since`."""
        ...

    def download(self, ride: RemoteRide) -> bytes | None:
        """A FIT file or a zip archive with one. None: there is none (yet)."""
        ...


class SecretStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


class MemoryStore:
    """For tests."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class KeyringStore:
    """The OS keychain. There is deliberately no fallback to a plain file."""

    def __init__(self) -> None:
        if type(keyring.get_keyring()).__module__.startswith("keyrings.alt"):
            raise SyncError("The system has no secure keychain.")

    def get(self, key: str) -> str | None:
        try:
            return keyring.get_password(_KEYRING_SERVICE, key)
        except keyring.errors.KeyringError as e:
            raise SyncError(f"Keychain not available: {type(e).__name__}") from e

    def set(self, key: str, value: str) -> None:
        try:
            keyring.set_password(_KEYRING_SERVICE, key, value)
        except Exception as e:  # backends also leak OS errors
            raise SyncError(f"Keychain not available: {type(e).__name__}") from e

    def delete(self, key: str) -> None:
        try:
            keyring.delete_password(_KEYRING_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass  # not there
        except keyring.errors.KeyringError as e:
            raise SyncError(f"Keychain not available: {type(e).__name__}") from e


def account_key(service: str, account: str) -> str:
    """Keychain entry. account: keeps the logins of several riders apart."""
    return f"{service}-{account}" if account else service


def forget(store: SecretStore, services: tuple[str, ...], account: str) -> None:
    """Delete the logins of a removed rider, without telling the services."""
    if not account:
        raise ValueError("no account")
    for service in services:
        store.delete(account_key(service, account))


def fetch_file(http: httpx.Client, url: str) -> bytes | None:
    """A file behind a link of a service, without any login. None: not there (any more)."""
    try:
        for _ in range(_REDIRECTS + 1):
            parts = urlsplit(url)
            if parts.scheme != "https" or not parts.hostname:
                raise SyncError("The link to the file is not valid.")
            with http.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                return read_limited(response)
    except httpx.HTTPError as e:
        raise Transient(f"The file could not be loaded ({type(e).__name__}).") from None
    except (ValueError, httpx.InvalidURL):
        raise SyncError("The link to the file is not valid.") from None
    raise SyncError("The link to the file is not valid.")


def read_limited(response: httpx.Response) -> bytes | None:
    """The body of a streamed response. None: there is no file."""
    if response.status_code in (403, 404, 410):
        return None
    if response.status_code == 429 or response.status_code >= 500:
        raise Transient(f"The file could not be loaded (error {response.status_code}).")
    if response.status_code != 200:
        raise SyncError(f"The file could not be loaded (error {response.status_code}).")
    data = bytearray()
    for part in response.iter_bytes():
        data += part
        if len(data) > MAX_FILE_BYTES:
            raise SyncError("The file is too large.")
    return bytes(data)


def http_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": "indoor-trainer-app"})
