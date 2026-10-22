"""The connections file (port of ConnectionStore.swift).

All mutations persist before the in-memory snapshot changes; a failed write leaves both the file
and the snapshot as they were. A file that exists but can't be read is never overwritten: every
mutation loads first and fails the same way. One store object per file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Iterable

from .. import paths
from ..i18n import _
from .connection import CURRENT_SCHEMA_VERSION, Connection, new_id
from .errors import (DuplicateID, InvalidStore, MissingConnection, StaleImportPlan, UnsupportedSchema)
from .import_report import ImportAction, ImportPlan

log = logging.getLogger(__name__)

#: (original bytes, old schema version) -> bytes in the current schema.
Migration = Callable[[bytes, int], bytes]

SORT_NAME = "name"
SORT_RECENT = "recent"


def encode_document(connections: Iterable[Connection]) -> bytes:
    """The file's bytes: pretty-printed, sorted keys, like the Mac app's JSONEncoder."""
    document = {"schemaVersion": CURRENT_SCHEMA_VERSION, "connections": [c.to_json() for c in connections]}
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, separators=(",", " : ")).encode()


def natural_key(text: str) -> list:
    """Finder-like order (Swift's localizedStandardCompare): case-insensitive, numbers by value."""
    return [(0, int(part)) if part.isdigit() else (1, part) for part in re.split(r"(\d+)", text.casefold()) if part]


class ConnectionStore:
    def __init__(self, path: Path | str | None = None, migration: Migration | None = None):
        self.path = Path(path) if path is not None else paths.connections_file()
        self._migration = migration
        self._connections: list[Connection] = []
        self._loaded = False
        self._listeners: dict[int, Callable[[], None]] = {}
        self._next_listener = 0
        #: Copy of the original file kept by the last schema migration.
        self.last_migration_backup: Path | None = None

    # MARK: Snapshot and listeners

    @property
    def connections(self) -> list[Connection]:
        """A copy of the snapshot; change connections through the store."""
        return [connection.copy() for connection in self._connections]

    @property
    def import_backup_path(self) -> Path:
        """The file as it was before the last import changed it (one rolling copy)."""
        return self.path.with_suffix(".before-import.json")

    @property
    def file_exists(self) -> bool:
        return self.path.exists()

    def get(self, connection_id: str) -> Connection | None:
        for connection in self._connections:
            if connection.id == connection_id:
                return connection.copy()
        return None

    def add_listener(self, callback: Callable[[], None]) -> int:
        """Calls `callback()` after every load and change; returns a token for remove_listener."""
        self._next_listener += 1
        self._listeners[self._next_listener] = callback
        return self._next_listener

    def remove_listener(self, token: int) -> None:
        self._listeners.pop(token, None)

    def _changed(self) -> None:
        for callback in list(self._listeners.values()):
            try:
                callback()
            except Exception:  # noqa: BLE001 - one broken listener must not stop the others
                log.exception("connection store listener failed")

    # MARK: Loading

    def load(self) -> list[Connection]:
        if not self.path.exists():
            self._connections = []
            self._loaded = True
            self._changed()
            return self.connections
        original = self.path.read_bytes()
        try:
            obj = json.loads(original)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise InvalidStore(f"not JSON: {error}") from None
        version = obj.get("schemaVersion", 0) if isinstance(obj, dict) else 0
        if not isinstance(version, int) or isinstance(version, bool):
            raise InvalidStore("schemaVersion is not an integer")
        if version > CURRENT_SCHEMA_VERSION:
            raise UnsupportedSchema(version)
        if version < CURRENT_SCHEMA_VERSION:
            # Preserve the exact original bytes even if a migration hook fails afterwards.
            backup = self.path.with_name(f"{self.path.name}.v{version}.{str(uuid.uuid4()).upper()}.bak")
            _atomic_write(backup, original)
            self.last_migration_backup = backup
            data = self._migration(original, version) if self._migration else _migrate_legacy(original, version)
        else:
            data = original
        connections = _decode_document(data)
        _validate(connections)
        if data != original:
            _atomic_write(self.path, encode_document(connections))
        self._connections = connections
        self._loaded = True
        self._changed()
        return self.connections

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # MARK: Mutations

    def save(self) -> None:
        self._ensure_loaded()
        self._write(self._connections)

    def replace_all(self, connections: list[Connection]) -> None:
        """The only mutation that doesn't load an existing file first."""
        copies = [connection.copy() for connection in connections]
        self._write(copies)
        self._connections = copies
        self._loaded = True
        self._changed()

    def insert(self, connection: Connection) -> None:
        self._ensure_loaded()
        if any(existing.id == connection.id for existing in self._connections):
            raise DuplicateID(connection.id)
        self.replace_all(self._connections + [connection])

    def update(self, connection: Connection) -> None:
        self._ensure_loaded()
        index = self._index(connection.id)
        updated = list(self._connections)
        updated[index] = connection
        self.replace_all(updated)

    def modify(self, connection_id: str, change: Callable[[Connection], None]) -> Connection:
        """Changes the stored copy in place (read, change, write), so small updates such as the
        last-connected date never overwrite other changes with a stale copy."""
        self._ensure_loaded()
        connection = self._connections[self._index(connection_id)].copy()
        change(connection)
        connection.id = connection_id
        self.update(connection)
        return connection.copy()

    def delete(self, connection_id: str) -> None:
        self._ensure_loaded()
        self._index(connection_id)
        self.replace_all([c for c in self._connections if c.id != connection_id])

    def duplicate(self, connection_id: str, name: str | None = None) -> Connection:
        self._ensure_loaded()
        copy = self._connections[self._index(connection_id)].copy()
        copy.id = new_id()
        copy.name = name if name is not None else _("{name} copy").format(name=copy.name)
        copy.last_connected = None
        copy.import_source = None
        self.insert(copy)
        return copy.copy()

    def apply(self, plan: ImportPlan, selected_ids: set[str] | None = None) -> None:
        """Call only after the person confirmed the preview. None selects all; an empty set none."""
        self._ensure_loaded()
        updated = list(self._connections)
        for entry in plan.entries:
            if selected_ids is not None and entry.connection.id not in selected_ids:
                continue
            if entry.action is ImportAction.UNCHANGED:
                continue
            index = next((i for i, c in enumerate(updated) if c.id == entry.connection.id), None)
            if entry.previous is not None:
                if index is None or updated[index] != entry.previous:
                    raise StaleImportPlan()
                updated[index] = entry.connection
            else:
                source = entry.connection.import_source
                if index is not None or (source is not None and any(
                        c.import_source is not None and c.import_source.kind == source.kind
                        and c.import_source.unique_id == source.unique_id for c in updated)):
                    raise StaleImportPlan()
                updated.append(entry.connection)
        if updated == self._connections:
            return
        if self.path.exists():
            _atomic_write(self.import_backup_path, self.path.read_bytes())
        self.replace_all(updated)

    # MARK: Search

    def search(self, query: str = "", sort: str = SORT_NAME) -> list[Connection]:
        return [c.copy() for c in self.filter(self._connections, query, sort)]

    @staticmethod
    def filter(connections: Iterable[Connection], query: str, sort: str = SORT_NAME) -> list[Connection]:
        """Search over name, host, user, domain, notes and tags; SORT_RECENT puts the last used first."""
        needle = query.strip().casefold()

        def matches(c: Connection) -> bool:
            return not needle or any(needle in text.casefold()
                                     for text in [c.name, c.host, c.username, c.domain, c.notes, *c.tags])

        result = [c for c in connections if matches(c)]
        result.sort(key=lambda c: (natural_key(c.name), c.id))
        if sort == SORT_RECENT:
            # Stable: equal dates keep the name order. Never used goes last.
            result.sort(key=lambda c: -(c.last_connected if c.last_connected is not None else float("-inf")))
        return result

    # MARK: Internals

    def _index(self, connection_id: str) -> int:
        for index, connection in enumerate(self._connections):
            if connection.id == connection_id:
                return index
        raise MissingConnection(connection_id)

    def _write(self, connections: list[Connection]) -> None:
        _validate(connections)
        _atomic_write(self.path, encode_document(connections))


def _validate(connections: list[Connection]) -> None:
    seen: set[str] = set()
    for connection in connections:
        if connection.id in seen:
            raise DuplicateID(connection.id)
        seen.add(connection.id)
        connection.validate()


def _decode_document(data: bytes) -> list[Connection]:
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InvalidStore(f"not JSON: {error}") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("connections"), list):
        raise InvalidStore("no connections list")
    version = obj.get("schemaVersion")
    if version != CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchema(version if isinstance(version, int) else 0)
    return [Connection.from_json(item) for item in obj["connections"]]


def _migrate_legacy(data: bytes, version: int) -> bytes:
    """Version 0: a bare list of connection objects (or {"connections": [...]}) with missing keys."""
    if version != 0:
        raise UnsupportedSchema(version)
    obj = json.loads(data)
    records = obj if isinstance(obj, list) else obj.get("connections") if isinstance(obj, dict) else None
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise InvalidStore("legacy file without connection records")
    defaults = Connection(name="", host="placeholder.example").to_json()
    migrated = [{**defaults, **old, "schemaVersion": CURRENT_SCHEMA_VERSION} for old in records]
    return json.dumps({"schemaVersion": CURRENT_SCHEMA_VERSION, "connections": migrated}).encode()


def _atomic_write(path: Path, data: bytes) -> None:
    """Writes a temporary file next to `path` and renames it over, so readers see old or new, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
