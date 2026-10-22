"""Remmina import, the Linux counterpart of the Jump import.

Remmina keeps one INI file per connection (group [remmina]) in ~/.local/share/remmina (the Flatpak
in ~/.var/app/org.remmina.Remmina/data/remmina, very old versions in ~/.remmina). RDP and VNC
entries are taken over; what maps is mapped, the rest is listed in the report. Re-imports match on
the file name. Remmina's encrypted passwords are never read.
"""

from __future__ import annotations

import configparser
import os
import re
from pathlib import Path

from .. import paths
from ..i18n import _
from .connection import (AudioPlaybackMode, Connection, ConnectionProtocol, DriveMapping, ImportSource,
                         now_reference)
from .errors import ConnectionStoreError
from .import_report import (ConnectionImportReport, ImportAction, ImportEntry, ImportFieldIssue, ImportPlan,
                            ImportReport)

KIND = "remmina"
MAX_FILE_SIZE = 1024 * 1024
_SECRET_KEYS = {"password", "gateway_password", "ssh_password", "ssh_passphrase", "ssh_tunnel_password",
                "ssh_tunnel_passphrase"}
_INTEGER = re.compile(r"^[+-]?\d+$")

# Remmina's RemminaProtocolResolutionMode and view modes.
RES_USE_CUSTOM, RES_USE_CLIENT, RES_USE_INITIAL_WINDOW_SIZE = 0, 1, 2
FULLSCREEN_VIEW_MODES = {2, 3, 4}  # fullscreen, scrolled fullscreen, viewport fullscreen
SCALE_DYNAMIC_RESOLUTION = 2


def default_directories() -> list[Path]:
    """Where Remmina keeps its files, the usual place first; only existing folders. In the Flatpak,
    XDG_DATA_HOME is this app's own folder; the desktop's is HOST_XDG_DATA_HOME (only set when the
    desktop sets XDG_DATA_HOME) or ~/.local/share."""
    data_home = os.environ.get("HOST_XDG_DATA_HOME" if paths.flatpak_id() else "XDG_DATA_HOME")
    base = Path(data_home) if data_home and os.path.isabs(data_home) else Path.home() / ".local/share"
    candidates = [base / "remmina", Path.home() / ".var/app/org.remmina.Remmina/data/remmina",
                  Path.home() / ".remmina"]
    return [path for path in candidates if path.is_dir()]


def password_notice() -> str:
    return _("Remmina’s saved password isn’t imported; you’ll be asked for it on the first connection.")


class _Reader:
    """String values of the [remmina] group; empty values count as not set."""

    def __init__(self, values: dict[str, str], report: ConnectionImportReport):
        self.values = values
        self.report = report
        self.consumed: set[str] = set()

    def raw(self, key: str) -> str | None:
        value = self.values.get(key)
        if value is None:
            return None
        if key not in self.consumed:
            self.consumed.add(key)
            if value != "":
                self.report.imported_fields.append(key)
        return value if value != "" else None

    def string(self, key: str) -> str | None:
        return self.raw(key)

    def integer(self, key: str) -> int | None:
        value = self.raw(key)
        if value is None:
            return None
        if not _INTEGER.match(value.strip()):
            self.ignore(key, _("Not an integer; not imported."))
            return None
        return int(value.strip())

    def boolean(self, key: str) -> bool | None:
        value = self.raw(key)
        if value is None:
            return None
        lowered = value.strip().lower()
        if lowered in ("1", "true"):
            return True
        if lowered in ("0", "false"):
            return False
        self.ignore(key, _("Not a yes/no value; not imported."))
        return None

    def ignore(self, key: str, reason: str) -> None:
        self.consumed.add(key)
        self.report.imported_fields = [name for name in self.report.imported_fields if name != key]
        self.report.ignored_fields.append(ImportFieldIssue(key, reason))

    def finish(self) -> ConnectionImportReport:
        for key in sorted(self.values):
            # Empty values mean "not set"; Remmina writes its many off switches as 0.
            if key in self.consumed or self.values[key] in ("", "0", "false"):
                continue
            if key in _SECRET_KEYS:
                self.ignore(key, _("Saved password; not imported."))
            else:
                self.ignore(key, _("No equivalent; not imported."))
        return self.report


def parse_server(server: str, default_port: int) -> tuple[str, int] | None:
    """`host`, `host:port`, `[v6]:port` or a bare IPv6 address; None if unusable."""
    value = server.strip()
    if value.startswith("["):
        close = value.find("]")
        if close < 0:
            return None
        host, rest = value[1:close], value[close + 1:]
        if not rest:
            port = default_port
        elif rest.startswith(":") and _INTEGER.match(rest[1:]):
            port = int(rest[1:])
        else:
            return None
    elif value.count(":") == 1:
        host, _sep, port_text = value.partition(":")
        if not _INTEGER.match(port_text):
            return None
        port = int(port_text)
    else:
        host, port = value, default_port
    if not host or any(ch.isspace() or ch in "/\0" for ch in host) or not 1 <= port <= 65535:
        return None
    return host, port


class RemminaImporter:
    def __init__(self, directory: Path | str):
        self.directory = Path(directory)

    def plan(self, existing: list[Connection], imported_at: float | None = None) -> ImportPlan:
        """Reads only the selected directory; raises OSError if it can't be listed."""
        imported_at = imported_at if imported_at is not None else now_reference()
        files = sorted((p for p in self.directory.iterdir() if p.suffix.lower() == ".remmina" and p.is_file()),
                       key=lambda p: p.name)
        grouped: dict[str, list[Connection]] = {}
        for connection in existing:
            source = connection.import_source
            if source is not None and source.kind == KIND:
                grouped.setdefault(source.unique_id, []).append(connection)
        entries: list[ImportEntry] = []
        reports: list[ConnectionImportReport] = []
        for file in files:
            report = ConnectionImportReport(file_name=file.name, unique_id=file.name)
            values = _read_group(file)
            if values is None:
                report.warnings.append(_("Not a readable Remmina file; nothing planned."))
                reports.append(report)
                continue
            reader = _Reader(values, report)
            entry = self._plan_one(file, reader, grouped, imported_at)
            if entry is not None:
                entries.append(entry)
            reports.append(reader.finish())
        return ImportPlan(entries=entries, report=ImportReport(connections=reports, source="Remmina"))

    def _plan_one(self, file: Path, reader: _Reader, grouped: dict[str, list[Connection]],
                  imported_at: float) -> ImportEntry | None:
        report = reader.report
        matches = grouped.get(file.name, [])
        if len(matches) > 1:
            report.warnings.append(_("Several saved connections belong to this Remmina connection; clean them "
                                     "up first. Not imported."))
            return None
        previous = matches[0] if matches else None
        protocol_name = (reader.string("protocol") or "").upper()
        if protocol_name == "RDP":
            transport, default_port = ConnectionProtocol.RDP, 3389
        elif protocol_name == "VNC":
            transport, default_port = ConnectionProtocol.VNC, 5900
        else:
            report.warnings.append(_("Only RDP and VNC connections are imported; this one is not."))
            if protocol_name:
                reader.ignore("protocol", _("Protocol not supported."))
            return None
        address = parse_server(reader.string("server") or "", default_port)
        if address is None:
            report.warnings.append(_("Host missing or invalid; not imported."))
            if "server" in reader.values and reader.values["server"]:
                reader.ignore("server", _("Not a host name or address."))
            return None
        host, port = address
        connection = previous.copy() if previous else Connection(name=host, host=host, port=port, protocol=transport)
        connection.host, connection.port, connection.protocol = host, port, transport
        connection.name = (reader.string("name") or "").strip() or host
        username = reader.string("username") or ""
        domain = reader.string("domain") or ""
        if "\\" in username and not domain:
            domain, username = username.split("\\", 1)
        connection.username, connection.domain = username, domain
        if (group := reader.string("group")) is not None:
            connection.tags = [part.strip() for part in group.split("/") if part.strip()]
        if transport is ConnectionProtocol.RDP:
            _map_rdp(connection, reader)
        if reader.values.get("password"):
            report.warnings.append(password_notice())  # finish() lists the key as not imported
        connection.import_source = ImportSource(
            kind=KIND, unique_id=file.name, file_name=file.name,
            imported_at=previous.import_source.imported_at if previous and previous.import_source else imported_at)
        try:
            connection.validate()
        except ConnectionStoreError:
            report.warnings.append(_("Settings out of range; not imported."))
            return None
        action = (ImportAction.CREATE if previous is None
                  else ImportAction.UNCHANGED if connection == previous else ImportAction.UPDATE)
        report.connection_name = connection.name
        report.action = action
        return ImportEntry(action=action, connection=connection, previous=previous)


def _read_group(file: Path) -> dict[str, str] | None:
    try:
        if file.stat().st_size > MAX_FILE_SIZE:
            return None
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    parser = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",),
                                       comment_prefixes=("#", ";"), inline_comment_prefixes=None)
    parser.optionxform = str  # keys are case-sensitive in Remmina
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    if not parser.has_section("remmina"):
        return None
    return {key: value.strip() for key, value in parser.items("remmina")}


def _map_rdp(connection: Connection, reader: _Reader) -> None:
    display, redirection = connection.display, connection.redirection
    security, advanced = connection.security, connection.advanced
    mode = reader.integer("resolution_mode")
    width, height = reader.integer("resolution_width"), reader.integer("resolution_height")
    if mode == RES_USE_CUSTOM or (mode is None and width and height):
        if width and height:
            display.match_screen_resolution = False
            display.fixed_width, display.fixed_height = width, height
    elif mode in (RES_USE_CLIENT, RES_USE_INITIAL_WINDOW_SIZE):
        display.match_screen_resolution = True
    elif mode is not None:
        reader.ignore("resolution_mode", _("Unknown Remmina value; the default stays."))
    if (scale := reader.integer("scale")) is not None:
        display.dynamic_resolution = scale == SCALE_DYNAMIC_RESOLUTION
    if (view := reader.integer("viewmode")) is not None:
        display.start_fullscreen = view in FULLSCREEN_VIEW_MODES
    if (flag := reader.boolean("multimon")) is not None:
        display.use_all_monitors = flag

    if (flag := reader.boolean("disableclipboard")) is not None:
        redirection.clipboard = not flag
    if (sound := reader.string("sound")) is not None:
        # Newer versions append options: "local,quality:high".
        modes = {"local": AudioPlaybackMode.LOCAL, "remote": AudioPlaybackMode.REMOTE, "off": AudioPlaybackMode.OFF}
        mode_name = sound.split(",", 1)[0].strip().lower()
        if mode_name in modes:
            redirection.audio_playback = modes[mode_name]
        else:
            reader.ignore("sound", _("Unknown Remmina value; the default stays."))
    if (microphone := reader.string("microphone")) is not None:
        redirection.microphone = microphone.strip() not in ("0", "false", "off")
    if (flag := reader.boolean("shareprinter")) is not None:
        redirection.printers = flag
    if (folder := reader.string("sharefolder")) is not None:
        name = Path(folder.rstrip("/")).name or "Remmina"
        redirection.drive_redirection = True
        if not any(drive.local_path == folder for drive in redirection.drives):
            redirection.drives.append(DriveMapping(name=name, local_path=folder))

    if (flag := reader.boolean("cert_ignore")) is not None:
        security.ignore_certificate_errors = flag
    if (protocol := reader.string("security")) is not None:
        value = protocol.strip().lower()
        if value in ("tls", "rdp"):
            security.disable_nla = True
        elif value in ("nla", "ext", "negotiate"):
            security.disable_nla = False
        else:
            reader.ignore("security", _("Unknown Remmina value; the default stays."))
    if (flag := reader.boolean("console")) is not None:
        security.console_session = flag

    if (shell := reader.string("exec")) is not None:
        advanced.alternate_shell = shell
    if (directory := reader.string("execpath")) is not None:
        advanced.working_dir = directory
    if (routing := reader.string("loadbalanceinfo")) is not None:
        advanced.load_balance_info = routing
    if (gateway := reader.string("gateway_server")) is not None:
        advanced.gateway_hostname = gateway
        reader.report.warnings.append(_("RD Gateway saved, but Weitblick Remote can’t connect through a "
                                        "gateway yet."))
