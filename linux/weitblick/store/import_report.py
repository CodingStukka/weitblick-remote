"""Import plans and reports, shared by the Jump and Remmina importers (port of ImportReport.swift).

An importer only reads its source and returns an ImportPlan: one entry per importable connection
(create, update or unchanged, matched to earlier imports by ImportSource kind + unique id) and a
report of what was taken over and what not. Nothing is stored until the person confirms the
preview and the plan goes to ConnectionStore.apply().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .connection import Connection


@dataclass(frozen=True)
class ImportFieldIssue:
    field: str
    reason: str


class ImportAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    UNCHANGED = "unchanged"


@dataclass
class ConnectionImportReport:
    """What happened to one source file. Values from the source are never put into messages."""

    file_name: str
    connection_name: str | None = None
    unique_id: str | None = None
    #: None: not imported.
    action: ImportAction | None = None
    imported_fields: list[str] = field(default_factory=list)
    ignored_fields: list[ImportFieldIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    connections: list[ConnectionImportReport] = field(default_factory=list)
    #: "Jump", "Remmina": for the readable summary.
    source: str = "Jump"

    @property
    def warning_count(self) -> int:
        return sum(len(item.warnings) for item in self.connections)

    @property
    def human_readable(self) -> str:
        """A plain-text summary (English, not localized), e.g. for a log or a details view."""
        lines = [f"{self.source} import ({self.source} files only read)",
                 f"Files: {len(self.connections)}; notes: {self.warning_count}"]
        for item in self.connections:
            action = item.action.value if item.action else "not imported"
            lines.append(f"\n{item.file_name}: {item.connection_name or 'unreadable'} [{action}]")
            lines.append("  Imported: " + ", ".join(sorted(item.imported_fields)))
            for issue in sorted(item.ignored_fields, key=lambda issue: issue.field):
                lines.append(f"  Not imported {issue.field}: {issue.reason}")
            for warning in item.warnings:
                lines.append(f"  Note: {warning}")
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ImportEntry:
    action: ImportAction
    connection: Connection
    #: The stored connection this entry replaces (None for a new one).
    previous: Connection | None

    @property
    def id(self) -> str:
        return self.connection.id


@dataclass(frozen=True)
class ImportPlan:
    entries: list[ImportEntry]
    report: ImportReport

    @property
    def create_count(self) -> int:
        return sum(1 for entry in self.entries if entry.action is ImportAction.CREATE)

    @property
    def update_count(self) -> int:
        return sum(1 for entry in self.entries if entry.action is ImportAction.UPDATE)

    @property
    def unchanged_count(self) -> int:
        return sum(1 for entry in self.entries if entry.action is ImportAction.UNCHANGED)
