"""The saved connection and its settings (port of ConnectionStore/Connection.swift).

The JSON form is the macOS app's connections.json, so a file copied from a Mac opens here and the
other way round: camelCase keys, UUIDs as uppercase strings, dates as seconds since 2001-01-01
(Swift's `deferredToDate`), optionals left out when unset. Keys this app doesn't know (newer Mac
versions, the Mac keyboard rules) are kept and written back unchanged.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .errors import InvalidConnection, InvalidStore, UnsupportedSchema

#: Seconds between the Unix epoch and Apple's reference date (2001-01-01 00:00:00 UTC).
REFERENCE_DATE_OFFSET = 978_307_200.0


def now_reference() -> float:
    """The current time as seconds since 2001-01-01 UTC, the form dates are stored in."""
    return datetime.now(timezone.utc).timestamp() - REFERENCE_DATE_OFFSET


def reference_to_datetime(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds + REFERENCE_DATE_OFFSET, timezone.utc)


def datetime_to_reference(moment: datetime) -> float:
    return moment.timestamp() - REFERENCE_DATE_OFFSET


def new_id() -> str:
    """A connection id: an uppercase UUID string, as Swift encodes UUIDs."""
    return str(uuid.uuid4()).upper()


def normalized_id(value: str) -> str:
    """Uppercase canonical form; raises ValueError for anything that isn't a UUID."""
    return str(uuid.UUID(value)).upper()


def normalized_fingerprint(fingerprint: str) -> str:
    """Hex digits only, lowercase: `AA:BB…`, `aabb…` and `AA BB …` are the same fingerprint."""
    return "".join(ch for ch in fingerprint.lower() if ch in "0123456789abcdef")


class ConnectionProtocol(str, Enum):
    RDP = "rdp"
    VNC = "vnc"


class AudioPlaybackMode(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"
    OFF = "off"


class AgentAccess(str, Enum):
    """A connection's "AI agent access" (MCP): ask in a permission prompt, always allow, or never."""

    ASK = "ask"
    ALWAYS = "always"
    NEVER = "never"


# MARK: - Decoding helpers

class _Reader:
    """Typed access to one JSON object; missing keys give the default, wrong types fail the store."""

    def __init__(self, obj: Any, where: str):
        if not isinstance(obj, dict):
            raise InvalidStore(f"{where} is not an object")
        self.obj = obj
        self.where = where
        self.used: set[str] = set()

    def _get(self, key: str) -> Any:
        self.used.add(key)
        return self.obj.get(key)

    def bool(self, key: str, default: bool) -> bool:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise InvalidStore(f"{self.where}.{key} is not a boolean")
        return value

    def int(self, key: str, default: int | None) -> int | None:
        value = self._get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            raise InvalidStore(f"{self.where}.{key} is not an integer")
        return int(value)

    def float(self, key: str) -> float | None:
        value = self._get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidStore(f"{self.where}.{key} is not a number")
        return float(value)

    def str(self, key: str, default: str | None) -> str | None:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, str):
            raise InvalidStore(f"{self.where}.{key} is not a string")
        return value

    def strings(self, key: str) -> list[str]:
        value = self._get(key)
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise InvalidStore(f"{self.where}.{key} is not a list of strings")
        return list(value)

    def list(self, key: str) -> list:
        value = self._get(key)
        if value is None:
            return []
        if not isinstance(value, list):
            raise InvalidStore(f"{self.where}.{key} is not a list")
        return value

    def obj_or_none(self, key: str) -> Any:
        return self._get(key)

    def enum(self, key: str, kind: type[Enum], default: Enum, lenient: bool = False) -> Any:
        value = self._get(key)
        if value is None:
            return default
        try:
            return kind(value)
        except ValueError:
            if lenient:
                return default
            raise InvalidStore(f"{self.where}.{key} has an unknown value") from None

    def extra(self) -> dict:
        """Keys nobody read: kept so they survive a save."""
        return {key: copy.deepcopy(value) for key, value in self.obj.items() if key not in self.used}


def _put(target: dict, key: str, value: Any) -> None:
    """encodeIfPresent: optionals are left out when unset."""
    if value is not None:
        target[key] = value


def _with_extra(encoded: dict, extra: dict) -> dict:
    merged = copy.deepcopy(extra)
    merged.update(encoded)
    return merged


# MARK: - Settings groups

@dataclass
class DisplaySettings:
    dynamic_resolution: bool = True
    match_screen_resolution: bool = True
    #: One remote pixel per screen pixel on HiDPI screens ("Retina" on the Mac).
    retina: bool = False
    fixed_width: int = 1024
    fixed_height: int = 768
    start_fullscreen: bool = False
    use_all_monitors: bool = False
    monitor_count: int = 1
    #: None means automatic; Jump stores this as zero.
    desktop_scale_factor: int | None = None
    extra: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, obj: Any) -> DisplaySettings:
        r = _Reader(obj, "display")
        result = cls(
            dynamic_resolution=r.bool("dynamicResolution", True),
            match_screen_resolution=r.bool("matchScreenResolution", True),
            retina=r.bool("retina", False),
            fixed_width=r.int("fixedWidth", 1024),
            fixed_height=r.int("fixedHeight", 768),
            start_fullscreen=r.bool("startFullscreen", False),
            use_all_monitors=r.bool("useAllMonitors", False),
            monitor_count=r.int("monitorCount", 1),
            desktop_scale_factor=r.int("desktopScaleFactor", None),
        )
        result.extra = r.extra()
        return result

    def to_json(self) -> dict:
        encoded = {
            "dynamicResolution": self.dynamic_resolution,
            "matchScreenResolution": self.match_screen_resolution,
            "retina": self.retina,
            "fixedWidth": self.fixed_width,
            "fixedHeight": self.fixed_height,
            "startFullscreen": self.start_fullscreen,
            "useAllMonitors": self.use_all_monitors,
            "monitorCount": self.monitor_count,
        }
        _put(encoded, "desktopScaleFactor", self.desktop_scale_factor)
        return _with_extra(encoded, self.extra)

    @property
    def follows_window(self) -> bool:
        """The remote desktop follows the window size (initially and on every resize)."""
        return self.match_screen_resolution

    @property
    def resizes_with_window(self) -> bool:
        """Resizes the remote desktop while the session runs."""
        return self.match_screen_resolution and self.dynamic_resolution


@dataclass
class DriveMapping:
    """A local folder shared into the session as \\\\tsclient\\<name>."""

    name: str
    local_path: str
    enabled: bool = True
    #: The session can't share read-only; such mappings are not shared at all.
    read_only: bool = False
    source_id: str | None = None
    extra: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, obj: Any) -> DriveMapping:
        r = _Reader(obj, "drive")
        result = cls(
            name=r.str("name", ""),
            local_path=r.str("localPath", ""),
            enabled=r.bool("enabled", True),
            read_only=r.bool("readOnly", False),
            source_id=r.str("sourceID", None),
        )
        result.extra = r.extra()
        return result

    def to_json(self) -> dict:
        encoded = {"name": self.name, "localPath": self.local_path, "enabled": self.enabled,
                   "readOnly": self.read_only}
        _put(encoded, "sourceID", self.source_id)
        return _with_extra(encoded, self.extra)


@dataclass
class RedirectionSettings:
    clipboard: bool = True
    audio_playback: AudioPlaybackMode = AudioPlaybackMode.LOCAL
    microphone: bool = False
    audio_input_device: str | None = None
    printers: bool = True
    default_printer: str | None = None
    #: Master switch, independent of each drive's enabled flag.
    drive_redirection: bool = False
    drives: list[DriveMapping] = field(default_factory=list)
    extra: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, obj: Any) -> RedirectionSettings:
        r = _Reader(obj, "redirection")
        result = cls(
            clipboard=r.bool("clipboard", True),
            audio_playback=r.enum("audioPlayback", AudioPlaybackMode, AudioPlaybackMode.LOCAL),
            microphone=r.bool("microphone", False),
            audio_input_device=r.str("audioInputDevice", None),
            printers=r.bool("printers", True),
            default_printer=r.str("defaultPrinter", None),
            drive_redirection=r.bool("driveRedirection", False),
            drives=[DriveMapping.from_json(item) for item in r.list("drives")],
        )
        result.extra = r.extra()
        return result

    def to_json(self) -> dict:
        encoded = {
            "clipboard": self.clipboard,
            "audioPlayback": self.audio_playback.value,
            "microphone": self.microphone,
            "printers": self.printers,
            "driveRedirection": self.drive_redirection,
            "drives": [drive.to_json() for drive in self.drives],
        }
        _put(encoded, "audioInputDevice", self.audio_input_device)
        _put(encoded, "defaultPrinter", self.default_printer)
        return _with_extra(encoded, self.extra)

    def shared_drives(self) -> list[tuple[str, str]]:
        """(share name, local folder) of the drives the session shares: with the master switch on,
        enabled and writable only (a read-only mapping is not shared rather than made writable).
        `~` is expanded."""
        import os

        if not self.drive_redirection:
            return []
        return [(drive.name, os.path.expanduser(drive.local_path)) for drive in self.drives
                if drive.enabled and not drive.read_only and drive.name and drive.local_path]


def parse_layout_id(notation: str) -> int | None:
    """Windows notation `00000407`, `0x407` or `407` (hex) -> KLID, None if invalid."""
    digits = notation[2:] if notation.lower().startswith("0x") else notation
    if not digits or len(digits) > 8:
        return None
    try:
        return int(digits, 16)
    except ValueError:
        return None


def format_layout_id(klid: int) -> str:
    """Eight hex digits, as Windows (and the Mac app's JSON) writes a KLID."""
    return f"{klid & 0xFFFFFFFF:08x}"


class TextInput(str, Enum):
    """How typed text reaches Windows (Linux-only key `textInput`)."""

    #: The app decides per key (scancodes when the layouts match, else Unicode).
    SMART = "smart"
    #: Every printable character typed without Ctrl/Alt/Super goes out as Unicode, for a remote
    #: layout that differs from the local one (the Mac app's `unicodeTextInput`).
    UNICODE = "unicode"
    #: Always scancodes (games, layout-exact shortcuts).
    SCANCODES = "scancodes"


@dataclass
class KeyboardSettings:
    """The keyboard part of a connection. The Linux app uses `textInput` and `layoutOverride`; the
    rest (the Mac shortcut mode, ⌥ strategy, rules, …) stays in `raw` and is written back as it was.
    `unicodeTextInput` is kept in sync with `textInput` so the Mac app reads the same choice."""

    text_input: TextInput = TextInput.SMART
    #: Windows keyboard layout (KLID) for the session; None = derived from the local layout.
    layout_override: int | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def unicode_text_input(self) -> bool:
        return self.text_input is TextInput.UNICODE

    @unicode_text_input.setter
    def unicode_text_input(self, value: bool) -> None:
        if value:
            self.text_input = TextInput.UNICODE
        elif self.text_input is TextInput.UNICODE:
            self.text_input = TextInput.SMART

    @classmethod
    def from_json(cls, obj: Any) -> KeyboardSettings:
        r = _Reader(obj, "keyboard")
        unicode_input = r.bool("unicodeTextInput", False)
        fallback = TextInput.UNICODE if unicode_input else TextInput.SMART
        text_input = r.enum("textInput", TextInput, fallback, lenient=True)
        notation = r.str("layoutOverride", None)
        raw = r.extra()
        return cls(text_input=text_input,
                   layout_override=parse_layout_id(notation) if notation is not None else None, raw=raw)

    def to_json(self) -> dict:
        encoded = {"textInput": self.text_input.value, "unicodeTextInput": self.unicode_text_input}
        if self.layout_override is not None:
            encoded["layoutOverride"] = format_layout_id(self.layout_override)
        return _with_extra(encoded, self.raw)


@dataclass
class SecuritySettings:
    ignore_certificate_errors: bool = False
    trusted_certificate_fingerprints: list[str] = field(default_factory=list)
    disable_nla: bool = False
    console_session: bool = False
    #: RDP authentication level: 0 = allow, 1 = require, 2 = warn; kept for .rdp round trips.
    authentication_level: int = 2
    prompt_for_credentials: bool = True
    #: Whether AI agents (MCP) may use this connection.
    agent_access: AgentAccess = AgentAccess.ASK
    extra: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, obj: Any) -> SecuritySettings:
        r = _Reader(obj, "security")
        result = cls(
            ignore_certificate_errors=r.bool("ignoreCertificateErrors", False),
            trusted_certificate_fingerprints=r.strings("trustedCertificateFingerprints"),
            disable_nla=r.bool("disableNLA", False),
            console_session=r.bool("consoleSession", False),
            authentication_level=r.int("authenticationLevel", 2),
            prompt_for_credentials=r.bool("promptForCredentials", True),
            # An unknown value (a newer app version) falls back to asking.
            agent_access=r.enum("agentAccess", AgentAccess, AgentAccess.ASK, lenient=True),
        )
        result.extra = r.extra()
        return result

    def to_json(self) -> dict:
        return _with_extra({
            "ignoreCertificateErrors": self.ignore_certificate_errors,
            "trustedCertificateFingerprints": list(self.trusted_certificate_fingerprints),
            "disableNLA": self.disable_nla,
            "consoleSession": self.console_session,
            "authenticationLevel": self.authentication_level,
            "promptForCredentials": self.prompt_for_credentials,
            "agentAccess": self.agent_access.value,
        }, self.extra)

    def trusts(self, fingerprint: str) -> bool:
        """Compares hex digits only, so `AA:BB…`, `aabb…` and `AA BB …` are the same fingerprint."""
        wanted = normalized_fingerprint(fingerprint)
        return bool(wanted) and any(normalized_fingerprint(known) == wanted
                                    for known in self.trusted_certificate_fingerprints)


@dataclass
class AdvancedSettings:
    alternate_shell: str | None = None
    working_dir: str | None = None
    load_balance_info: str | None = None
    wake_on_lan_mac_addresses: list[str] = field(default_factory=list)
    gateway_ref: str | None = None
    gateway_hostname: str | None = None
    #: Windows drive selectors from .rdp, not local paths. Never turn '*' into local drive access.
    rdp_drive_store_direct: str | None = None
    extra: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, obj: Any) -> AdvancedSettings:
        r = _Reader(obj, "advanced")
        result = cls(
            alternate_shell=r.str("alternateShell", None),
            working_dir=r.str("workingDir", None),
            load_balance_info=r.str("loadBalanceInfo", None),
            wake_on_lan_mac_addresses=r.strings("wakeOnLANMACAddresses"),
            gateway_ref=r.str("gatewayRef", None),
            gateway_hostname=r.str("gatewayHostname", None),
            rdp_drive_store_direct=r.str("rdpDriveStoreDirect", None),
        )
        result.extra = r.extra()
        return result

    def to_json(self) -> dict:
        encoded: dict = {"wakeOnLANMACAddresses": list(self.wake_on_lan_mac_addresses)}
        _put(encoded, "alternateShell", self.alternate_shell)
        _put(encoded, "workingDir", self.working_dir)
        _put(encoded, "loadBalanceInfo", self.load_balance_info)
        _put(encoded, "gatewayRef", self.gateway_ref)
        _put(encoded, "gatewayHostname", self.gateway_hostname)
        _put(encoded, "rdpDriveStoreDirect", self.rdp_drive_store_direct)
        return _with_extra(encoded, self.extra)


@dataclass
class ImportSource:
    """Where an imported connection came from; re-imports match on (kind, unique_id)."""

    unique_id: str
    file_name: str
    #: "jump" or "remmina".
    kind: str = "jump"
    #: Seconds since 2001-01-01 UTC.
    imported_at: float = field(default_factory=now_reference)
    extra: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, obj: Any) -> ImportSource:
        r = _Reader(obj, "importSource")
        result = cls(kind=r.str("kind", "jump"), unique_id=r.str("uniqueID", ""),
                     file_name=r.str("fileName", ""), imported_at=r.float("importedAt") or 0.0)
        result.extra = r.extra()
        return result

    def to_json(self) -> dict:
        return _with_extra({"kind": self.kind, "uniqueID": self.unique_id, "fileName": self.file_name,
                            "importedAt": self.imported_at}, self.extra)


# MARK: - Connection

CURRENT_SCHEMA_VERSION = 1


@dataclass
class Connection:
    name: str
    host: str
    port: int = 3389
    username: str = ""
    domain: str = ""
    protocol: ConnectionProtocol = ConnectionProtocol.RDP
    id: str = field(default_factory=new_id)
    display: DisplaySettings = field(default_factory=DisplaySettings)
    redirection: RedirectionSettings = field(default_factory=RedirectionSettings)
    keyboard: KeyboardSettings = field(default_factory=KeyboardSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    advanced: AdvancedSettings = field(default_factory=AdvancedSettings)
    tags: list[str] = field(default_factory=list)
    #: Seconds since 2001-01-01 UTC (see reference_to_datetime), None = never.
    last_connected: float | None = None
    notes: str = ""
    import_source: ImportSource | None = None
    schema_version: int = CURRENT_SCHEMA_VERSION
    extra: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, obj: Any) -> Connection:
        r = _Reader(obj, "connection")
        raw_id = r.str("id", None)
        if raw_id is None:
            raise InvalidStore("connection without id")
        try:
            connection_id = normalized_id(raw_id)
        except ValueError:
            raise InvalidStore(f"invalid connection id {raw_id!r}") from None
        source = r.obj_or_none("importSource")
        result = cls(
            schema_version=r.int("schemaVersion", CURRENT_SCHEMA_VERSION),
            id=connection_id,
            name=r.str("name", ""),
            host=r.str("host", ""),
            port=r.int("port", 3389),
            username=r.str("username", ""),
            domain=r.str("domain", ""),
            protocol=r.enum("protocol", ConnectionProtocol, ConnectionProtocol.RDP),
            display=DisplaySettings.from_json(r.obj_or_none("display") or {}),
            redirection=RedirectionSettings.from_json(r.obj_or_none("redirection") or {}),
            keyboard=KeyboardSettings.from_json(r.obj_or_none("keyboard") or {}),
            security=SecuritySettings.from_json(r.obj_or_none("security") or {}),
            advanced=AdvancedSettings.from_json(r.obj_or_none("advanced") or {}),
            tags=r.strings("tags"),
            last_connected=r.float("lastConnected"),
            notes=r.str("notes", ""),
            import_source=ImportSource.from_json(source) if source is not None else None,
        )
        result.extra = r.extra()
        return result

    def to_json(self) -> dict:
        encoded = {
            "schemaVersion": self.schema_version,
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "domain": self.domain,
            "protocol": self.protocol.value,
            "display": self.display.to_json(),
            "redirection": self.redirection.to_json(),
            "keyboard": self.keyboard.to_json(),
            "security": self.security.to_json(),
            "advanced": self.advanced.to_json(),
            "tags": list(self.tags),
            "notes": self.notes,
        }
        _put(encoded, "lastConnected", self.last_connected)
        _put(encoded, "importSource", self.import_source.to_json() if self.import_source else None)
        return _with_extra(encoded, self.extra)

    def copy(self) -> Connection:
        return copy.deepcopy(self)

    @property
    def last_connected_datetime(self) -> datetime | None:
        return reference_to_datetime(self.last_connected) if self.last_connected is not None else None

    @property
    def vnc_url(self) -> str | None:
        """vnc://host:port for VNC connections (no credentials in the URL), for a VNC viewer."""
        if self.protocol is not ConnectionProtocol.VNC:
            return None
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"vnc://{host}:{self.port}"

    def validate(self) -> None:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise UnsupportedSchema(self.schema_version)
        if not self.host.strip() or not 1 <= self.port <= 65535:
            raise InvalidConnection(self.id)
        display = self.display
        if display.fixed_width <= 0 or display.fixed_height <= 0 or display.monitor_count <= 0:
            raise InvalidConnection(self.id)
        if display.desktop_scale_factor is not None and not 100 <= display.desktop_scale_factor <= 500:
            raise InvalidConnection(self.id)
