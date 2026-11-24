"""Shared by all services: errors, HTTP client, secret storage."""

from __future__ import annotations

import logging
from typing import Protocol

import httpx
import keyring
import keyring.errors

_KEYRING_SERVICE = "pywatts"
TIMEOUT_S = 15.0

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


def http_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": "indoor-trainer-app"})
