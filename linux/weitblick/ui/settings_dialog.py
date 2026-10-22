"""Settings: sessions, keyboard capture, AI agents (MCP) and where the data lives (port of
SettingsWindowController)."""

from __future__ import annotations

import logging

from gi.repository import Adw, Gio, GLib, Gtk

from .. import paths
from ..i18n import _

log = logging.getLogger(__name__)

CAPTURE_MODES = ["fullscreen", "always", "never"]


class SettingsDialog(Adw.PreferencesDialog):
    __gtype_name__ = "WBSettingsDialog"

    def __init__(self, app):
        super().__init__(title=_("Settings"))
        self.app = app
        settings = app.settings

        general = Adw.PreferencesPage(title=_("General"), icon_name="preferences-system-symbolic")
        sessions = Adw.PreferencesGroup(title=_("Sessions"))
        own = Adw.SwitchRow(title=_("Open sessions in separate windows"),
                            subtitle=_("Instead of tabs next to the overview, e.g. for several monitors"),
                            active=settings.sessions_in_own_windows)
        own.connect("notify::active", lambda row, _p: setattr(settings, "sessions_in_own_windows", row.get_active()))
        sessions.add(own)
        confirm = Adw.SwitchRow(title=_("Ask before closing a session"), active=settings.confirm_close_session)
        confirm.connect("notify::active", lambda row, _p: setattr(settings, "confirm_close_session", row.get_active()))
        sessions.add(confirm)
        general.add(sessions)

        keyboard = Adw.PreferencesGroup(
            title=_("Keyboard"),
            description=_("Ctrl+Alt+Super shortcuts always stay in Weitblick Remote: Ctrl+Alt+Super+F full screen, "
                          "Ctrl+Alt+Super+←/→ and 1…9 switch tabs."))
        capture = Adw.ComboRow(title=_("Send system shortcuts to Windows"),
                               subtitle=_("Alt+Tab, Super and other shortcuts the desktop would take"),
                               model=Gtk.StringList.new([_("In full screen"), _("Always"), _("Never")]))
        current = settings.system_shortcut_capture
        capture.set_selected(CAPTURE_MODES.index(current) if current in CAPTURE_MODES else 0)
        capture.connect("notify::selected", lambda row, _p: setattr(settings, "system_shortcut_capture",
                                                                    CAPTURE_MODES[row.get_selected()]))
        keyboard.add(capture)
        general.add(keyboard)

        data = Adw.PreferencesGroup(title=_("Data"))
        connections = Adw.ActionRow(title=_("Connections"), subtitle=str(paths.connections_file()),
                                    subtitle_selectable=True)
        show = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                          tooltip_text=_("Open Folder"))
        show.connect("clicked", lambda *_: self._open_uri(paths.config_dir().as_uri()))
        connections.add_suffix(show)
        data.add(connections)
        data.add(Adw.ActionRow(title=_("Passwords"), subtitle=_("In the keyring (Secret Service), on this computer only")))
        general.add(data)
        self.add(general)

        agents = Adw.PreferencesPage(title=_("AI Agents"), icon_name="weitblick-agent-symbolic")
        try:
            from ..agent.ui import AgentSettingsGroup
            agents.add(AgentSettingsGroup(app))
        except ImportError:
            log.warning("agent settings unavailable", exc_info=True)
            agents.add(Adw.PreferencesGroup(description=_("Agent control isn’t available in this build.")))
        self.add(agents)

    def _open_uri(self, uri: str) -> None:
        paths.config_dir().mkdir(parents=True, exist_ok=True)
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as error:
            log.warning("opening %s failed: %s", uri, error.message)
