"""App-wide preferences (port of AppSettings.swift), a small JSON file instead of UserDefaults.

    settings = AppSettings()                  # ~/.config/weitblick-remote/settings.json
    settings.confirm_close_session = False    # saved at once, listeners hear "confirmCloseSession"

The keys are the Mac app's UserDefaults keys. `weitblick-mcp` reads agentControlEnabled from the
same file (agent_control_enabled_in_file), so it never needs the app running to know it is off.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from .. import paths

log = logging.getLogger(__name__)

SESSIONS_IN_OWN_WINDOWS = "sessionsInOwnWindows"
CONFIRM_CLOSE_SESSION = "confirmCloseSession"
OVERVIEW_SORT = "overviewSort"
AGENT_CONTROL_ENABLED = "agentControlEnabled"
SYSTEM_SHORTCUT_CAPTURE = "systemShortcutCapture"

SHORTCUT_CAPTURE_FULLSCREEN = "fullscreen"
SHORTCUT_CAPTURE_ALWAYS = "always"
SHORTCUT_CAPTURE_NEVER = "never"

#: key -> (default, allowed values or None for any value of the default's type)
_SCHEMA: dict[str, tuple[Any, tuple | None]] = {
    SESSIONS_IN_OWN_WINDOWS: (False, None),
    CONFIRM_CLOSE_SESSION: (True, None),
    OVERVIEW_SORT: ("name", ("name", "recent")),
    AGENT_CONTROL_ENABLED: (False, None),
    SYSTEM_SHORTCUT_CAPTURE: (SHORTCUT_CAPTURE_FULLSCREEN,
                              (SHORTCUT_CAPTURE_FULLSCREEN, SHORTCUT_CAPTURE_ALWAYS, SHORTCUT_CAPTURE_NEVER)),
}


def _valid(key: str, value: Any) -> bool:
    default, allowed = _SCHEMA[key]
    if type(value) is not type(default):
        return False
    return allowed is None or value in allowed


def _read(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        log.warning("settings file %s unreadable, using defaults: %s", path, error)
        return {}
    return obj if isinstance(obj, dict) else {}


def agent_control_enabled_in_file(path: Path | str | None = None) -> bool:
    """The master switch for agent control as stored, for processes without an AppSettings."""
    value = _read(Path(path) if path is not None else paths.settings_file()).get(AGENT_CONTROL_ENABLED)
    return value is True


class AppSettings:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else paths.settings_file()
        #: Everything in the file, including keys of other versions (written back unchanged).
        self._values: dict = _read(self.path)
        self._listeners: dict[int, Callable[[str], None]] = {}
        self._next_listener = 0

    def get(self, key: str) -> Any:
        value = self._values.get(key)
        return value if key in _SCHEMA and value is not None and _valid(key, value) else _SCHEMA[key][0]

    def set(self, key: str, value: Any) -> None:
        if key not in _SCHEMA:
            raise KeyError(key)
        if not _valid(key, value):
            raise ValueError(f"invalid value for {key}: {value!r}")
        if self._values.get(key) == value and key in self._values:
            return
        self._values[key] = value
        self._save()
        self._notify(key)

    def reload(self) -> None:
        """Re-reads the file (another process may have changed it); listeners hear changed keys."""
        before = {key: self.get(key) for key in _SCHEMA}
        self._values = _read(self.path)
        for key in _SCHEMA:
            if self.get(key) != before[key]:
                self._notify(key)

    def add_listener(self, callback: Callable[[str], None]) -> int:
        """Calls `callback(key)` after a setting changed; returns a token for remove_listener."""
        self._next_listener += 1
        self._listeners[self._next_listener] = callback
        return self._next_listener

    def remove_listener(self, token: int) -> None:
        self._listeners.pop(token, None)

    def _notify(self, key: str) -> None:
        for callback in list(self._listeners.values()):
            try:
                callback(key)
            except Exception:  # noqa: BLE001
                log.exception("settings listener failed")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self._values, indent=2, sort_keys=True).encode()
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    # Typed accessors

    @property
    def sessions_in_own_windows(self) -> bool:
        """Sessions open as separate windows instead of tabs next to the overview (multi-monitor work)."""
        return self.get(SESSIONS_IN_OWN_WINDOWS)

    @sessions_in_own_windows.setter
    def sessions_in_own_windows(self, value: bool) -> None:
        self.set(SESSIONS_IN_OWN_WINDOWS, value)

    @property
    def confirm_close_session(self) -> bool:
        """Ask before closing a session tab disconnects it."""
        return self.get(CONFIRM_CLOSE_SESSION)

    @confirm_close_session.setter
    def confirm_close_session(self, value: bool) -> None:
        self.set(CONFIRM_CLOSE_SESSION, value)

    @property
    def overview_sort(self) -> str:
        """"name" or "recent"."""
        return self.get(OVERVIEW_SORT)

    @overview_sort.setter
    def overview_sort(self, value: str) -> None:
        self.set(OVERVIEW_SORT, value)

    @property
    def agent_control_enabled(self) -> bool:
        """Master switch for agent control over MCP; off = no socket at all."""
        return self.get(AGENT_CONTROL_ENABLED)

    @agent_control_enabled.setter
    def agent_control_enabled(self, value: bool) -> None:
        self.set(AGENT_CONTROL_ENABLED, value)

    @property
    def system_shortcut_capture(self) -> str:
        """When system shortcuts (Alt+Tab, Super, …) go to Windows: "fullscreen", "always", "never"."""
        return self.get(SYSTEM_SHORTCUT_CAPTURE)

    @system_shortcut_capture.setter
    def system_shortcut_capture(self, value: str) -> None:
        self.set(SYSTEM_SHORTCUT_CAPTURE, value)
