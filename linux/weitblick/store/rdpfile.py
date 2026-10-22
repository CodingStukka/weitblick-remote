"""Microsoft .rdp files: import and export (port of RDPFile.swift).

Lines are `key:type:value` (type s = string, i = integer). Files are UTF-16 LE with a BOM when
mstsc writes them, sometimes UTF-8. Import never takes over a saved password; export writes only
settings that mean the same thing everywhere, never passwords, fingerprints or local paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..i18n import _
from .connection import AudioPlaybackMode, Connection, ConnectionProtocol
from .errors import ConnectionStoreError
from .import_report import ImportFieldIssue

_NEWLINES = "\n\r\x0b\x0c\x85  "
_INTEGER = re.compile(r"^[+-]?\d+$")


class RDPFileError(Exception):
    """kind: "invalidEncoding", "missingAddress", "invalidAddress", "invalidValue" (with the key),
    "unsupportedProtocol"."""

    def __init__(self, kind: str, key: str | None = None):
        super().__init__(kind, key)
        self.kind = kind
        self.key = key

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RDPFileError) and (self.kind, self.key) == (other.kind, other.key)

    def __hash__(self) -> int:
        return hash((self.kind, self.key))


class RDPFileEncoding(Enum):
    UTF8 = "utf-8"
    UTF16LE = "utf-16-le"


@dataclass
class RDPFileImport:
    connection: Connection
    ignored_fields: list[ImportFieldIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class _FieldReader:
    def __init__(self, fields: dict[str, tuple[str, str]]):
        self.fields = fields
        self.consumed: set[str] = set()

    def string(self, key: str) -> str | None:
        if key not in self.fields:
            return None
        self.consumed.add(key)
        kind, value = self.fields[key]
        if kind != "s" or "\0" in value:
            raise RDPFileError("invalidValue", key)
        return value

    def integer(self, key: str) -> int | None:
        if key not in self.fields:
            return None
        self.consumed.add(key)
        kind, value = self.fields[key]
        text = value.strip(" \t")
        if kind != "i" or not _INTEGER.match(text):
            raise RDPFileError("invalidValue", key)
        return int(text)

    def boolean(self, key: str) -> bool | None:
        value = self.integer(key)
        if value is None:
            return None
        if value not in (0, 1):
            raise RDPFileError("invalidValue", key)
        return value == 1


def read(path: Path | str) -> RDPFileImport:
    """Reads a .rdp file; the connection is named after the file."""
    path = Path(path)
    return decode(path.read_bytes(), name=path.stem)


def decode(data: bytes, name: str = "Imported RDP") -> RDPFileImport:
    try:
        if data.startswith(b"\xff\xfe"):
            text = data[2:].decode("utf-16-le")
        elif data.startswith(b"\xef\xbb\xbf"):
            text = data[3:].decode("utf-8")
        else:
            text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise RDPFileError("invalidEncoding") from None

    fields: dict[str, tuple[str, str]] = {}
    warnings: list[str] = []
    for index, line in enumerate(text.splitlines()):
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            warnings.append(_("Line {number} unreadable; ignored.").format(number=index + 1))
            continue
        key = parts[0].strip(" \t").lower()
        if key in ("password 51", "password"):
            warnings.append(_("Saved password in the file ignored; you’ll be asked for it when connecting."))
            continue
        if key in fields:
            warnings.append(_("Field {key} appears twice; the last value applies.").format(key=key))
        fields[key] = (parts[1].lower(), parts[2])

    reader = _FieldReader(fields)
    address = reader.string("full address")
    if not address:
        raise RDPFileError("missingAddress")
    host, port = _parse_address(address)
    connection = Connection(name=name, host=host, port=port)
    connection.username = reader.string("username") or ""
    connection.domain = reader.string("domain") or ""
    display = connection.display
    if (value := reader.integer("desktopwidth")) is not None:
        display.fixed_width = value
    if (value := reader.integer("desktopheight")) is not None:
        display.fixed_height = value
    if (value := reader.integer("screen mode id")) is not None:
        if value not in (1, 2):
            raise RDPFileError("invalidValue", "screen mode id")
        display.start_fullscreen = value == 2
    if (flag := reader.boolean("dynamic resolution")) is not None:
        display.dynamic_resolution = flag
    if (flag := reader.boolean("use multimon")) is not None:
        display.use_all_monitors = flag
    redirection = connection.redirection
    if (flag := reader.boolean("redirectclipboard")) is not None:
        redirection.clipboard = flag
    if (value := reader.integer("audiomode")) is not None:
        # The .rdp code space is documented and differs from Jump's tentative one.
        modes = {0: AudioPlaybackMode.LOCAL, 1: AudioPlaybackMode.REMOTE, 2: AudioPlaybackMode.OFF}
        if value not in modes:
            raise RDPFileError("invalidValue", "audiomode")
        redirection.audio_playback = modes[value]
    if (flag := reader.boolean("audiocapturemode")) is not None:
        redirection.microphone = flag
    if (flag := reader.boolean("redirectprinters")) is not None:
        redirection.printers = flag
    advanced = connection.advanced
    advanced.rdp_drive_store_direct = reader.string("drivestoredirect")
    if advanced.rdp_drive_store_direct:
        warnings.append(_("The file’s drive selection (Windows drives) is not imported as local folders; "
                          "share folders yourself."))
    advanced.alternate_shell = reader.string("alternate shell")
    advanced.working_dir = reader.string("shell working directory")
    advanced.load_balance_info = reader.string("loadbalanceinfo")
    advanced.gateway_hostname = reader.string("gatewayhostname")
    security = connection.security
    if (value := reader.integer("authentication level")) is not None:
        if not 0 <= value <= 2:
            raise RDPFileError("invalidValue", "authentication level")
        security.authentication_level = value
        security.ignore_certificate_errors = value == 0
    if (flag := reader.boolean("prompt for credentials")) is not None:
        security.prompt_for_credentials = flag
    try:
        connection.validate()
    except ConnectionStoreError:
        raise RDPFileError("invalidAddress") from None
    ignored = [ImportFieldIssue(key, _("Setting not supported; not imported."))
               for key in sorted(fields) if key not in reader.consumed]
    return RDPFileImport(connection=connection, ignored_fields=ignored, warnings=warnings)


def encode(connection: Connection, encoding: RDPFileEncoding = RDPFileEncoding.UTF16LE) -> bytes:
    """Only explicitly supported settings; never passwords, fingerprints or local path names."""
    if connection.protocol is not ConnectionProtocol.RDP:
        raise RDPFileError("unsupportedProtocol")
    connection.validate()
    lines: list[str] = []

    def string(key: str, value: str | None) -> None:
        if value is None:
            return
        if any(ch in _NEWLINES or ch == "\0" for ch in value):
            raise RDPFileError("invalidValue", key)
        lines.append(f"{key}:s:{value}")

    def integer(key: str, value: int) -> None:
        lines.append(f"{key}:i:{value}")

    def boolean(key: str, value: bool) -> None:
        integer(key, 1 if value else 0)

    host = connection.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    display, redirection = connection.display, connection.redirection
    advanced, security = connection.advanced, connection.security
    string("full address", f"{host}:{connection.port}")
    string("username", connection.username)
    string("domain", connection.domain)
    integer("desktopwidth", display.fixed_width)
    integer("desktopheight", display.fixed_height)
    integer("screen mode id", 2 if display.start_fullscreen else 1)
    boolean("dynamic resolution", display.dynamic_resolution)
    boolean("use multimon", display.use_all_monitors)
    boolean("redirectclipboard", redirection.clipboard)
    integer("audiomode", {AudioPlaybackMode.LOCAL: 0, AudioPlaybackMode.REMOTE: 1,
                          AudioPlaybackMode.OFF: 2}[redirection.audio_playback])
    boolean("audiocapturemode", redirection.microphone)
    boolean("redirectprinters", redirection.printers)
    string("drivestoredirect", advanced.rdp_drive_store_direct or "")
    string("alternate shell", advanced.alternate_shell)
    string("shell working directory", advanced.working_dir)
    string("loadbalanceinfo", advanced.load_balance_info)
    string("gatewayhostname", advanced.gateway_hostname)
    integer("authentication level", 0 if security.ignore_certificate_errors else security.authentication_level)
    boolean("prompt for credentials", security.prompt_for_credentials)
    text = "\r\n".join(lines) + "\r\n"
    if encoding is RDPFileEncoding.UTF8:
        return text.encode("utf-8")
    return b"\xff\xfe" + text.encode("utf-16-le")


def _parse_address(value: str) -> tuple[str, int]:
    value = value.strip(" \t")
    if value.startswith("["):
        close = value.find("]")
        if close < 0:
            raise RDPFileError("invalidAddress")
        host = value[1:close]
        suffix = value[close + 1:]
        if not suffix:
            port = 3389
        elif suffix.startswith(":") and _INTEGER.match(suffix[1:]):
            port = int(suffix[1:])
        else:
            raise RDPFileError("invalidAddress")
    elif value.count(":") == 1:
        host, _sep, port_text = value.partition(":")
        if not _INTEGER.match(port_text):
            raise RDPFileError("invalidAddress")
        port = int(port_text)
    else:
        host, port = value, 3389
    if not host or any(ch.isspace() or ch in "/\0" for ch in host) or not 1 <= port <= 65535:
        raise RDPFileError("invalidAddress")
    return host, port
