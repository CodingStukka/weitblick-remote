"""The saved connections as the app sees them (port of ConnectionLibrary.swift): the store plus
the passwords in the credential store. Every change goes through the store first.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable

from ..i18n import _
from .connection import Connection, now_reference
from .credentials import (CredentialStore, CredentialStoreError, InMemoryCredentialStore,
                          SecretServiceCredentialStore, password_label)
from .errors import ConnectionStoreError, MissingConnection
from .import_report import ImportPlan
from .store import SORT_NAME, ConnectionStore

log = logging.getLogger(__name__)


class ConnectionLibrary:
    def __init__(self, store: ConnectionStore | None = None, credentials: CredentialStore | None = None):
        self.store = store if store is not None else ConnectionStore()
        self.credentials: CredentialStore = credentials if credentials is not None else SecretServiceCredentialStore()
        #: Set when the connections file exists but can't be read. The store then refuses every
        #: change, so the file is never overwritten.
        self.load_error: str | None = None

    @property
    def connections(self) -> list[Connection]:
        return self.store.connections

    @property
    def is_first_start(self) -> bool:
        """No connections file yet: the first start (an import is offered)."""
        return not self.store.file_exists

    def load(self) -> None:
        try:
            self.store.load()
            self.load_error = None
        except (ConnectionStoreError, OSError) as error:
            log.error("loading connections failed: %r", error)
            self.load_error = _("The connections in “{path}” couldn’t be read. The file stays unchanged.").format(
                path=self.store.path)

    def add_listener(self, callback: Callable[[], None]) -> int:
        return self.store.add_listener(callback)

    def remove_listener(self, token: int) -> None:
        self.store.remove_listener(token)

    def connection(self, connection_id: str) -> Connection | None:
        return self.store.get(connection_id)

    def find(self, name_or_id: str) -> Connection | None:
        """By id, else by exact name, else by case-insensitive name (agents pass either)."""
        text = name_or_id.strip()
        by_id = self.store.get(text.upper())
        if by_id is not None:
            return by_id
        connections = self.store.connections
        exact = [c for c in connections if c.name == text]
        if len(exact) == 1:
            return exact[0]
        folded = [c for c in connections if c.name.casefold() == text.casefold()]
        return folded[0] if len(folded) == 1 else None

    def search(self, query: str = "", sort: str = SORT_NAME) -> list[Connection]:
        return self.store.search(query, sort)

    def add(self, connection: Connection) -> None:
        self.store.insert(connection)

    def update(self, connection: Connection) -> None:
        self.store.update(connection)

    def modify(self, connection_id: str, change: Callable[[Connection], None]) -> Connection:
        return self.store.modify(connection_id, change)

    def delete(self, connection_ids: Iterable[str]) -> None:
        """Deletes the connections and their saved passwords."""
        for connection_id in list(connection_ids):
            self.store.delete(connection_id)
            try:
                self.delete_password(connection_id)
            except CredentialStoreError as error:
                log.error("deleting password failed: %s", error)

    def duplicate(self, connection_id: str) -> Connection:
        """A copy named "… copy" with the same saved password."""
        original = self.store.get(connection_id)
        if original is None:
            raise MissingConnection(connection_id)
        copy = self.store.duplicate(connection_id, _("{name} copy").format(name=original.name))
        password = self.password(connection_id)
        if password is not None:
            try:
                self.set_password(password, copy.id)
            except CredentialStoreError as error:
                log.error("copying password failed: %s", error)
        return copy

    def mark_connected(self, connection_id: str, when: float | None = None) -> None:
        """Records a successful connection for the "last used" order. Failure is only logged."""
        moment = when if when is not None else now_reference()
        try:
            self.store.modify(connection_id, lambda c: setattr(c, "last_connected", moment))
        except (ConnectionStoreError, OSError) as error:
            log.error("recording last connection failed: %r", error)

    def trust(self, fingerprint: str, connection_id: str, replacing: bool) -> None:
        """"Always trust": adds the fingerprint; `replacing` drops the previously trusted ones (the
        server has a new certificate)."""
        def change(connection: Connection) -> None:
            if replacing:
                connection.security.trusted_certificate_fingerprints = []
            if not connection.security.trusts(fingerprint):
                connection.security.trusted_certificate_fingerprints.append(fingerprint)
        self.store.modify(connection_id, change)

    def apply_import(self, plan: ImportPlan, selected_ids: set[str] | None = None) -> None:
        self.store.apply(plan, selected_ids)

    # MARK: Passwords

    def password(self, connection_id: str) -> str | None:
        """The saved password; None if there is none or the keyring refused (only logged)."""
        try:
            return self.credentials.get(connection_id)
        except CredentialStoreError as error:
            log.error("reading password failed: %s", error)
            return None

    def set_password(self, password: str, connection_id: str) -> None:
        connection = self.store.get(connection_id)
        self.credentials.set(password, connection_id, password_label(connection.name if connection else ""))

    def delete_password(self, connection_id: str) -> None:
        self.credentials.delete(connection_id)


__all__ = ["ConnectionLibrary", "InMemoryCredentialStore"]
