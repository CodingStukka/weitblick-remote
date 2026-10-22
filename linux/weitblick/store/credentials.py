"""Passwords per connection (port of CredentialStore.swift): the Secret Service (GNOME Keyring,
KWallet) through libsecret on the desktop, an in-memory store for tests.

Passwords live in the default collection (the login keyring), never in connections.json. Errors
carry no account names, passwords or item data.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

log = logging.getLogger(__name__)

SCHEMA_NAME = "nrw.neuhaus.WeitblickRemote"
ATTRIBUTE = "connection"


class CredentialStoreError(Exception):
    """The keyring failed or isn't available; the message is safe to log."""


class CredentialStore(Protocol):
    def get(self, connection_id: str) -> str | None: ...
    def set(self, password: str, connection_id: str, label: str = "") -> None: ...
    def delete(self, connection_id: str) -> None: ...


class SecretServiceCredentialStore:
    """libsecret, synchronous calls (they are quick; the GTK main loop keeps running behind D-Bus)."""

    def __init__(self, schema_name: str = SCHEMA_NAME):
        self.schema_name = schema_name
        self._schema = None

    def _secret(self):
        try:
            import gi

            gi.require_version("Secret", "1")
            from gi.repository import Secret
        except (ImportError, ValueError) as error:
            raise CredentialStoreError(
                "libsecret isn't available (install gir1.2-secret-1 and a keyring such as gnome-keyring)"
            ) from error
        if self._schema is None:
            self._schema = Secret.Schema.new(self.schema_name, Secret.SchemaFlags.NONE,
                                             {ATTRIBUTE: Secret.SchemaAttributeType.STRING})
        return Secret

    @staticmethod
    def _attributes(connection_id: str) -> dict[str, str]:
        return {ATTRIBUTE: connection_id.upper()}

    def get(self, connection_id: str) -> str | None:
        Secret = self._secret()
        try:
            return Secret.password_lookup_sync(self._schema, self._attributes(connection_id), None)
        except Exception as error:  # GLib.Error: no Secret Service, locked and dismissed, …
            raise CredentialStoreError(f"reading the password failed: {_safe(error)}") from None

    def set(self, password: str, connection_id: str, label: str = "") -> None:
        Secret = self._secret()
        try:
            ok = Secret.password_store_sync(self._schema, self._attributes(connection_id),
                                            Secret.COLLECTION_DEFAULT, label or "Weitblick Remote",
                                            password, None)
        except Exception as error:
            raise CredentialStoreError(f"saving the password failed: {_safe(error)}") from None
        if not ok:
            raise CredentialStoreError("saving the password failed")

    def delete(self, connection_id: str) -> None:
        Secret = self._secret()
        try:
            Secret.password_clear_sync(self._schema, self._attributes(connection_id), None)
        except Exception as error:
            raise CredentialStoreError(f"deleting the password failed: {_safe(error)}") from None


def _safe(error: Exception) -> str:
    """GLib errors name the domain and code; that is all that goes into messages."""
    domain = getattr(error, "domain", None)
    code = getattr(error, "code", None)
    return f"{domain}:{code}" if domain is not None else type(error).__name__


class InMemoryCredentialStore:
    """For tests and as a session-only fallback when no keyring is available."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._passwords: dict[str, str] = {}
        self.labels: dict[str, str] = {}

    def get(self, connection_id: str) -> str | None:
        with self._lock:
            return self._passwords.get(connection_id.upper())

    def set(self, password: str, connection_id: str, label: str = "") -> None:
        with self._lock:
            self._passwords[connection_id.upper()] = password
            self.labels[connection_id.upper()] = label

    def delete(self, connection_id: str) -> None:
        with self._lock:
            self._passwords.pop(connection_id.upper(), None)
            self.labels.pop(connection_id.upper(), None)


def password_label(connection_name: str) -> str:
    """The keyring item's label as people see it in Passwords and Keys (Seahorse)."""
    return f"Weitblick Remote: {connection_name}" if connection_name else "Weitblick Remote"
