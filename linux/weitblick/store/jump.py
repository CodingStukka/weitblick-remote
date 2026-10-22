"""Jump Desktop import (port of JumpImporter.swift).

Jump has no Linux version; this reads a folder of *.jump files copied from a Mac (on the Mac:
~/Library/Containers/com.p5sys.jump.mac.viewer/Data/Documents/JumpDesktop/Viewer/Servers). Only
that folder is read, nothing is written there. Passwords are not in the files.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from ..i18n import _
from .connection import (AudioPlaybackMode, Connection, ConnectionProtocol, DriveMapping, ImportSource,
                         now_reference)
from .errors import ConnectionStoreError
from .import_report import (ConnectionImportReport, ImportAction, ImportEntry, ImportFieldIssue, ImportPlan,
                            ImportReport)

KIND = "jump"
MAX_FILE_SIZE = 4 * 1024 * 1024

#: Jump enums whose values aren't documented: listed, not interpreted.
_UNKNOWN_ENUMS = {"ColorDepthCode", "RdpPerformanceFlags", "ConnectionTypeCode", "TypeCode", "OsTypeCode",
                  "OSTypeCode", "GestureProfileCode"}


def password_notice() -> str:
    """Every imported entry carries this warning; UIs may show it once instead of per entry."""
    return _("The password isn’t in the Jump file; you’ll be asked for it on the first connection "
             "(Jump’s keychain stays untouched).")


class _Reader:
    """Typed reads from one Jump object; records what was imported and what not."""

    def __init__(self, obj: dict, report: ConnectionImportReport):
        self.obj = obj
        self.report = report
        self.consumed: set[str] = set()

    def has(self, key: str) -> bool:
        return key in self.obj

    def read(self, key: str) -> Any:
        if key not in self.obj:
            return None
        if key not in self.consumed:
            self.consumed.add(key)
            self.report.imported_fields.append(key)
        return self.obj[key]

    def string(self, key: str) -> str | None:
        value = self.read(key)
        if value is None:
            return None
        if not isinstance(value, str):
            self.ignore(key, _("Not text; not imported."))
            return None
        return value

    def number(self, key: str) -> float | None:
        value = self.read(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            self.ignore(key, _("Not a number; not imported."))
            return None
        return float(value)

    def integer(self, key: str) -> int | None:
        value = self.number(key)
        if value is None:
            return None
        if value != int(value):
            self.ignore(key, _("Not an integer; not imported."))
            return None
        return int(value)

    def boolean(self, key: str) -> bool | None:
        value = self.read(key)
        if value is None:
            return None
        if not isinstance(value, bool):
            self.ignore(key, _("Not a yes/no value; not imported."))
            return None
        return value

    def array(self, key: str) -> list | None:
        value = self.read(key)
        if value is None:
            return None
        if not isinstance(value, list):
            self.ignore(key, _("Not a list; not imported."))
            return None
        return value

    def strings(self, key: str) -> list[str] | None:
        value = self.array(key)
        if value is None:
            return None
        if not all(isinstance(item, str) for item in value):
            self.ignore(key, _("Not a list of texts; not imported."))
            return None
        return list(value)

    def ignore(self, key: str, reason: str) -> None:
        self.consumed.add(key)
        self.report.imported_fields = [name for name in self.report.imported_fields if name != key]
        self.report.ignored_fields.append(ImportFieldIssue(key, reason))

    def finish(self) -> ConnectionImportReport:
        for key in sorted(self.obj):
            if key not in self.consumed:
                self.ignore(key, _("Undocumented Jump code; not interpreted, the default stays.")
                            if key in _UNKNOWN_ENUMS else _("No equivalent; not imported."))
        return self.report


class JumpImporter:
    def __init__(self, directory: Path | str):
        self.directory = Path(directory)

    def plan(self, existing: list[Connection], imported_at: float | None = None) -> ImportPlan:
        """Reads only the selected directory; raises OSError if it can't be listed."""
        imported_at = imported_at if imported_at is not None else now_reference()
        files = sorted((p for p in self.directory.iterdir() if p.suffix.lower() == ".jump"), key=lambda p: p.name)
        grouped: dict[str, list[Connection]] = {}
        for connection in existing:
            source = connection.import_source
            if source is not None and source.kind == KIND:
                grouped.setdefault(source.unique_id, []).append(connection)
        entries: list[ImportEntry] = []
        reports: list[ConnectionImportReport] = []
        seen: set[str] = set()
        for file in files:
            report = ConnectionImportReport(file_name=file.name)
            try:
                data = file.read_bytes()
                obj = json.loads(data) if len(data) <= MAX_FILE_SIZE else None
            except (OSError, ValueError):
                report.warnings.append(_("File unreadable or invalid; nothing planned."))
                reports.append(report)
                continue
            if not isinstance(obj, dict):
                report.warnings.append(_("Not a readable Jump file (no JSON object or larger than 4 MiB)."))
                reports.append(report)
                continue
            reader = _Reader(obj, report)
            entry = self._plan_one(file, reader, grouped, seen, imported_at)
            if entry is not None:
                entries.append(entry)
            reports.append(reader.finish())
        return ImportPlan(entries=entries, report=ImportReport(connections=reports, source="Jump"))

    def _plan_one(self, file: Path, reader: _Reader, grouped: dict[str, list[Connection]], seen: set[str],
                  imported_at: float) -> ImportEntry | None:
        report = reader.report
        unique_id = reader.string("UniqueId")
        if not unique_id:
            report.warnings.append(_("UniqueId missing; without it a later import couldn’t be matched. "
                                     "Not imported."))
            return None
        report.unique_id = unique_id
        if unique_id in seen:
            report.warnings.append(_("Duplicate UniqueId in the Jump folder; this file was not imported."))
            return None
        seen.add(unique_id)
        matches = grouped.get(unique_id, [])
        if len(matches) > 1:
            report.warnings.append(_("Several saved connections belong to this Jump connection; clean them up "
                                     "first. Not imported."))
            return None
        previous = matches[0] if matches else None
        protocol_code = reader.integer("ProtocolTypeCode")
        if protocol_code not in (0, 1):
            report.warnings.append(_("Unknown protocol (ProtocolTypeCode); not imported rather than guessed."))
            return None
        transport = ConnectionProtocol.RDP if protocol_code == 0 else ConnectionProtocol.VNC
        default_port = 3389 if transport is ConnectionProtocol.RDP else 5900
        host = reader.string("TcpHostName") or ""
        if not host:
            report.warnings.append(_("Host missing; not imported."))
            return None
        connection = previous.copy() if previous else Connection(name=host, host=host, port=default_port,
                                                                 protocol=transport)
        connection.host = host
        connection.protocol = transport
        connection.name = reader.string("DisplayName") or host
        port = reader.integer("TcpPort")
        connection.port = port if port is not None else default_port
        if reader.has("Username"):
            connection.username = reader.string("Username") or ""
        if reader.has("Domain"):
            connection.domain = reader.string("Domain") or ""
        _map_display(connection, reader)
        _map_redirection(connection, reader)
        _map_keyboard(connection, reader)
        _map_security(connection, reader)
        _map_advanced(connection, reader)
        if reader.has("Tags"):
            connection.tags = reader.strings("Tags") or []
        if reader.has("LastConnectedTime"):
            # NSDate seconds since 2001-01-01, NOT Unix seconds; zero means never.
            seconds = reader.number("LastConnectedTime") or 0.0
            jump_date = seconds if seconds > 0 else None
            # A connection made with Weitblick Remote after the last import must not be rolled back.
            dates = [d for d in (previous.last_connected if previous else None, jump_date) if d is not None]
            connection.last_connected = max(dates) if dates else None
        connection.import_source = ImportSource(
            kind=KIND, unique_id=unique_id, file_name=file.name,
            imported_at=previous.import_source.imported_at if previous and previous.import_source else imported_at)
        try:
            connection.validate()
        except ConnectionStoreError:
            report.warnings.append(_("File unreadable or invalid; nothing planned."))
            return None
        action = (ImportAction.CREATE if previous is None
                  else ImportAction.UNCHANGED if connection == previous else ImportAction.UPDATE)
        report.connection_name = connection.name
        report.action = action
        report.warnings.append(password_notice())
        return ImportEntry(action=action, connection=connection, previous=previous)


def _map_display(connection: Connection, reader: _Reader) -> None:
    display = connection.display
    if (flag := reader.boolean("UseDynamicResolutionUpdate")) is not None:
        display.dynamic_resolution = flag
    if (flag := reader.boolean("MatchScreenResolution")) is not None:
        display.match_screen_resolution = flag
    if (value := reader.integer("ResolutionWidth")) is not None:
        display.fixed_width = value
    if (value := reader.integer("ResolutionHeight")) is not None:
        display.fixed_height = value
    if (flag := reader.boolean("UseHIDPIResolution")) is not None:
        display.retina = flag
    if reader.has("DesktopScaleFactor"):
        factor = reader.integer("DesktopScaleFactor") or 0
        display.desktop_scale_factor = None if factor == 0 else factor
    if (flag := reader.boolean("StartInFullscreen")) is not None:
        display.start_fullscreen = flag
    if (flag := reader.boolean("UseAllMonitors")) is not None:
        display.use_all_monitors = flag
    if (value := reader.integer("MonitorCount")) is not None:
        display.monitor_count = value


def _map_redirection(connection: Connection, reader: _Reader) -> None:
    redirection = connection.redirection
    if (flag := reader.boolean("ClipboardRedirection")) is not None:
        redirection.clipboard = flag
    if (code := reader.integer("AudioPlaybackCode")) is not None:
        # Only 2 is corroborated by a local playback-on configuration. Not the .rdp code space.
        redirection.audio_playback = AudioPlaybackMode.LOCAL if code == 2 else AudioPlaybackMode.OFF
        if code == 2:
            reader.report.warnings.append(_("Audio: Jump’s code 2 read as “play on this computer”; please check."))
        else:
            reader.report.warnings.append(
                _("Unknown audio code {code}; audio playback turned off to be safe.").format(code=code))
            reader.ignore("AudioPlaybackCode", _("Unknown Jump value; playback off."))
    if reader.has("AudioInputDevice"):
        device = reader.string("AudioInputDevice")
        redirection.audio_input_device = device
        redirection.microphone = bool(device)
    if (flag := reader.boolean("RdpPrinterRedirection")) is not None:
        redirection.printers = flag
    if reader.has("DefaultPrinter"):
        redirection.default_printer = reader.string("DefaultPrinter")
    if (flag := reader.boolean("RdpDriveRedirection")) is not None:
        redirection.drive_redirection = flag
    mappings = reader.array("DriveMappings")
    if mappings is None:
        return
    drives: list[DriveMapping] = []
    for index, item in enumerate(mappings):
        label = f"DriveMappings[{index}]"
        if not isinstance(item, dict):
            reader.ignore(label, _("Not an object; drive not imported."))
            continue
        drive = _Reader(item, ConnectionImportReport(file_name=""))
        name = drive.string("DisplayName") or ""
        path = drive.string("LocalPath") or ""
        enabled = drive.boolean("Enabled") or False
        read_only = drive.boolean("IsReadOnly") or False
        source_id = drive.string("UniqueId")
        drive_report = drive.finish()
        for name_of_field in drive_report.imported_fields:
            reader.report.imported_fields.append(f"{label}.{name_of_field}")
        for issue in drive_report.ignored_fields:
            reader.ignore(f"{label}.{issue.field}", issue.reason)
        if not name or not path:
            reader.ignore(label, _("Name or path missing; drive not imported."))
            continue
        if read_only and enabled:
            reader.report.warnings.append(
                _("Drive “{name}” is read-only in Jump and is not shared (sharing needs write access).")
                .format(name=name))
        drives.append(DriveMapping(name=name, local_path=path, enabled=enabled, read_only=read_only,
                                   source_id=source_id))
    redirection.drives = drives


def _map_keyboard(connection: Connection, reader: _Reader) -> None:
    keyboard = connection.keyboard
    if (flag := reader.boolean("RdpUseUnicodeKeyboard")) is not None:
        keyboard.unicode_text_input = flag
    automatic = reader.boolean("KeyboardAutomaticLocaleDetection")
    if automatic is None or automatic:
        keyboard.layout_override = None
        if reader.has("KeyboardLocaleId"):
            reader.ignore("KeyboardLocaleId", _("Automatic layout detection takes precedence."))
    else:
        value = reader.integer("KeyboardLocaleId")
        if value is not None and 0 < value <= 0xFFFFFFFF:
            keyboard.layout_override = value
        else:
            keyboard.layout_override = None
            reader.report.warnings.append(
                _("No valid keyboard layout (KeyboardLocaleId); it is detected automatically."))
    if reader.has("KeyboardInputProfileId"):
        reader.ignore("KeyboardInputProfileId",
                      _("Reference to a Jump keyboard profile; the connection’s own keyboard settings apply."))


def _map_security(connection: Connection, reader: _Reader) -> None:
    security = connection.security
    if (flag := reader.boolean("IgnoreCertificateErrors")) is not None:
        security.ignore_certificate_errors = flag
    if (flag := reader.boolean("RdpDisableNLA")) is not None:
        security.disable_nla = flag
    if (flag := reader.boolean("RdpConsoleSession")) is not None:
        security.console_session = flag
    # Fingerprints trusted in Weitblick Remote since the last import stay; Jump's one is added.
    if reader.has("SslCertificateFingerPrint"):
        fingerprint = reader.string("SslCertificateFingerPrint")
        if fingerprint and not security.trusts(fingerprint):
            security.trusted_certificate_fingerprints.append(fingerprint)


def _map_advanced(connection: Connection, reader: _Reader) -> None:
    advanced = connection.advanced
    if reader.has("RdpAlternateShellPath"):
        advanced.alternate_shell = reader.string("RdpAlternateShellPath")
    if reader.has("RdpAlternateShellWorkingDir"):
        advanced.working_dir = reader.string("RdpAlternateShellWorkingDir")
    if reader.has("LoadBalancerInfo"):
        advanced.load_balance_info = reader.string("LoadBalancerInfo")
    if reader.has("RDGatewayUniqueId"):
        advanced.gateway_ref = reader.string("RDGatewayUniqueId")
        if advanced.gateway_ref is not None:
            reader.report.warnings.append(
                _("Gateway reference saved; the gateway settings themselves are not imported."))
    if reader.has("MacAddresses"):
        advanced.wake_on_lan_mac_addresses = reader.strings("MacAddresses") or []
