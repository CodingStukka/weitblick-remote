"""The agent audit log (~/.local/state/weitblick-remote/agent.log): one line per tool call and per
permission event. Port of AuditEntry.swift and AgentAuditLog.swift. Typed and pasted text appears by
length only; results (screens, clipboard contents) are never written."""

from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from .. import paths
from .mcp_core import encode_json

log = logging.getLogger(__name__)

#: Argument keys whose values are replaced by their length.
REDACTED_KEYS = frozenset({"text"})


def _timestamp(date: float, timezone: tzinfo | None) -> str:
    moment = datetime.fromtimestamp(date, timezone) if timezone else datetime.fromtimestamp(date).astimezone()
    text = moment.isoformat(timespec="milliseconds")
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def redacted(arguments: Any) -> Any:
    if not isinstance(arguments, dict):
        return arguments
    copy = dict(arguments)
    for key in REDACTED_KEYS:
        if isinstance(copy.get(key), str):
            copy[key] = f"<{len(copy[key])} characters>"
    return copy


@dataclass
class AuditEntry:
    #: Epoch seconds.
    date: float
    client: str
    #: "Name (host)", or None for tools without a connection.
    connection: str | None
    tool: str
    arguments: Any = field(default_factory=dict)
    #: None = success, else the error message.
    error: str | None = None
    #: Seconds.
    duration: float = 0.0

    def line(self, timezone: tzinfo | None = None) -> str:
        parts = [_timestamp(self.date, timezone), "client=" + encode_json(self.client)]
        if self.connection is not None:
            parts.append("connection=" + encode_json(self.connection))
        parts.append(f"tool={self.tool}")
        parts.append("args=" + encode_json(redacted(self.arguments)))
        parts.append("ok" if self.error is None else "error=" + encode_json(self.error))
        parts.append(f"{math.floor(self.duration * 1000 + 1e-6)}ms")
        return " ".join(parts)

    @staticmethod
    def event(message: str, date: float, timezone: tzinfo | None = None) -> str:
        """A free-form event line (server start, prompt answers)."""
        return _timestamp(date, timezone) + " " + message


class AuditLog:
    """Appends lines on a background thread so tool calls never wait for the disk. The file is
    created 0600: it names the user's computers."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.agent_log_file()
        # Lines, or an Event a flush() waits for.
        self._queue: queue.Queue[str | threading.Event] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def record(self, entry: AuditEntry) -> None:
        self._put(entry.line())

    def event(self, message: str) -> None:
        """A free-form event: server start, prompts and their answers, sessions ending."""
        self._put(AuditEntry.event(message, time.time()))

    def flush(self, timeout: float = 5.0) -> bool:
        """Waits until every line queued so far is written (tests, app exit)."""
        written = threading.Event()
        self._put(written)
        return written.wait(timeout)

    def _put(self, item: str | threading.Event) -> None:
        self._queue.put(item)
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="weitblick-agent-log", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=30)
            except queue.Empty:
                with self._lock:
                    if self._queue.empty():
                        self._thread = None  # idle: the next line starts a new writer
                        return
                continue
            if isinstance(item, threading.Event):
                item.set()
                continue
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    handle.write(item + "\n")
            except OSError as error:
                log.error("agent log not written: %s", error)
