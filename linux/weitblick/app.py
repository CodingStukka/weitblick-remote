"""The application: windows, the session registry, app actions and the agent host (port of
AppDelegate and WindowCoordinator)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, VERSION, paths  # noqa: E402
from .i18n import _  # noqa: E402
from .rdp.session import wait_for_all_sessions  # noqa: E402
from .store import ConnectionLibrary, ConnectionProtocol  # noqa: E402
from .store.settings import AppSettings  # noqa: E402
from .ui.activity import SessionActivity  # noqa: E402
from .ui.session_page import SessionPage  # noqa: E402
from .ui.window import MainWindow  # noqa: E402

log = logging.getLogger(__name__)

APP_ACCELS = {
    "win.fullscreen": ["<Control><Alt><Super>f"],
    "win.next-tab": ["<Control><Alt><Super>Right"],
    "win.previous-tab": ["<Control><Alt><Super>Left"],
    "win.close-tab": ["<Control><Alt><Super>w"],
}


class WeitblickApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Start without a window (for AI agents)"), None)
        self.add_main_option("version", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, _("Show the version"), None)
        self.library = ConnectionLibrary()
        self.settings = AppSettings()
        self.activity = SessionActivity()
        #: Every open session page, with or without a tab (agent sessions start without).
        self.pages: list[SessionPage] = []
        self.agent_host = None
        self._background = False
        self._held = False
        self._first_start_checked = False

    # MARK: Life cycle

    def do_handle_local_options(self, options: GLib.VariantDict) -> int:
        if options.contains("version"):
            print(f"{APP_NAME} {VERSION}")
            return 0
        if options.contains("background"):
            self._background = True
        return -1

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        self.library.load()
        self._install_style()
        self._install_actions()
        self.settings.add_listener(self._setting_changed)
        self._update_agent_host()

    def do_activate(self) -> None:
        if self._background:
            # Started by weitblick-mcp: no window, just the agent host.
            self._background = False
            if not self._held:
                self.hold()
                self._held = True
            return
        window = self.main_window(create=True)
        window.present()
        if not self._first_start_checked:
            self._first_start_checked = True
            GLib.idle_add(self._offer_first_import)
            # Test hook: WEITBLICK_DEBUG_CONNECT=<name or id> opens that connection at start.
            target = os.environ.get("WEITBLICK_DEBUG_CONNECT")
            connection = self.library.find(target) if target else None
            if connection is not None:
                GLib.idle_add(lambda: (self.open_connection(connection.id), GLib.SOURCE_REMOVE)[1])

    def do_open(self, files: list[Gio.File], _n_files: int, _hint: str) -> None:
        window = self.main_window(create=True)
        window.present()
        for file in files:
            path = file.get_path()
            if path:
                from .ui.importing import open_rdp_file
                open_rdp_file(self, window, Path(path))

    def do_shutdown(self) -> None:
        for page in list(self.pages):
            page.close()
        if self.agent_host is not None:
            self.agent_host.stop()
            self.agent_host = None
        # FreeRDP's threads use global state that exit() destroys under them.
        wait_for_all_sessions(5)
        Adw.Application.do_shutdown(self)

    def background_mode_wanted(self) -> bool:
        return self.settings.agent_control_enabled

    # MARK: Windows

    def main_window(self, create: bool = False) -> MainWindow | None:
        for window in self.get_windows():
            if isinstance(window, MainWindow) and window.has_overview:
                return window
        if not create:
            return None
        window = MainWindow(self, with_overview=True)
        window.connect("close-request", self._main_window_closing)
        return window

    def _main_window_closing(self, _window) -> bool:
        # With agent control on, the app keeps running for agents without the window.
        if self.settings.agent_control_enabled and not self._held:
            self.hold()
            self._held = True
        return False

    def window_of(self, page: SessionPage) -> MainWindow | None:
        root = page.get_root()
        return root if isinstance(root, MainWindow) else None

    def _install_style(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        theme = Gtk.IconTheme.get_for_display(display)
        theme.add_search_path(str(paths.icon_dir()))
        css = Gtk.CssProvider()
        css.load_from_path(str(paths.data_file("style.css")))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # MARK: Sessions

    def session_pages(self) -> list[SessionPage]:
        return list(self.pages)

    def _register(self, page: SessionPage) -> None:
        self.pages.append(page)
        page.connect("closed", self._page_closed)

    def _page_closed(self, page: SessionPage) -> None:
        if page in self.pages:
            self.pages.remove(page)
        if self.agent_host is not None:
            self.agent_host.page_closed(page)

    def open_connection(self, connection_id: str) -> None:
        """Connects, or shows the session that already runs for this connection."""
        connection = self.library.connection(connection_id)
        if connection is None:
            return
        if connection.protocol is ConnectionProtocol.VNC:
            self._open_vnc(connection)
            return
        # A running session is shown instead of opening a second one (Windows would log the first out),
        # also an agent's session without a tab.
        for page in self.pages:
            if page.connection_id == connection_id and (page.is_shown or page.is_agent_session):
                self.show_session_page(page, activate=True)
                return
        page = SessionPage(connection, self.library, self.settings, self.activity)
        self._register(page)
        if self.settings.sessions_in_own_windows:
            window = MainWindow(self, with_overview=False)
            window.set_default_size(*self._session_window_size(connection))
        else:
            window = self.main_window(create=True)
        window.add_session(page)
        window.present()
        if connection.display.start_fullscreen:
            window.fullscreen()
        # Started once the tab has its size: the view size decides the desktop size.
        GLib.idle_add(lambda: (page.start(), GLib.SOURCE_REMOVE)[1])

    def _session_window_size(self, connection) -> tuple[int, int]:
        display = connection.display
        if not display.follows_window:
            return (display.fixed_width, display.fixed_height + 90)
        return (1440, 900)

    def _open_vnc(self, connection) -> None:
        url = connection.vnc_url
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except GLib.Error as error:
            window = self.main_window()
            from .ui.dialogs import show_message
            show_message(window, _("Can’t open VNC"),
                         _("No app for VNC (vnc://) links is installed, e.g. Remmina or GNOME Connections.\n{error}")
                         .format(error=error.message))

    def open_agent_session(self, connection, width: int, height: int, show: bool, session_id: str) -> SessionPage:
        """A session for an agent: without a tab unless `show` (then a tab, without raising the window)."""
        page = SessionPage(connection, self.library, self.settings, self.activity, agent_desktop=(width, height),
                           session_id=session_id)
        self._register(page)
        if show:
            self.show_session_page(page, activate=False)
        page.start()
        return page

    def show_session_page(self, page: SessionPage, activate: bool = True) -> None:
        """Gives a windowless (agent) session a tab in the main window."""
        window = self.window_of(page)
        if window is None:
            window = self.main_window(create=True)
            window.add_session(page, select=activate)
            page.publish_agent_activity()
        if activate:
            window.show_page(page)
        elif not window.get_visible():
            window.set_visible(True)

    def watch_agent_session(self, connection_id: str) -> None:
        for page in self.pages:
            if page.connection_id == connection_id and page.agent_control is not None:
                self.show_session_page(page, activate=True)
                return

    def close_session_page(self, page: SessionPage) -> None:
        """Closes a session without asking (agent disconnect, idle timeout)."""
        window = self.window_of(page)
        if window is not None:
            tab = window.tab_view.get_page(page)
            page.close()
            if tab is not None:
                window.tab_view.close_page(tab)  # nothing to confirm anymore: the page has no session
        else:
            page.close()

    # MARK: Agent host

    def _setting_changed(self, key: str) -> None:
        if key == "agentControlEnabled":
            self._update_agent_host()
        elif key == "systemShortcutCapture":
            for window in self.get_windows():
                if isinstance(window, MainWindow):
                    window.update_shortcut_capture()
            action = self.lookup_action("shortcut-capture")
            if action is not None:
                action.set_state(GLib.Variant.new_string(self.settings.system_shortcut_capture))

    def _update_agent_host(self) -> None:
        enabled = self.settings.agent_control_enabled
        if enabled and self.agent_host is None:
            try:
                from .agent.host import AgentHost
            except ImportError:
                log.warning("agent host unavailable", exc_info=True)
                return
            self.agent_host = AgentHost(self)
            self.agent_host.start()
        elif not enabled and self.agent_host is not None:
            self.agent_host.stop()
            self.agent_host = None
            if self._held:
                self.release()
                self._held = False

    # MARK: Actions

    def _install_actions(self) -> None:
        def add(name, callback, parameter=None, state=None):
            action = (Gio.SimpleAction.new_stateful(name, parameter, state) if state is not None
                      else Gio.SimpleAction.new(name, parameter))
            action.connect("activate", callback)
            self.add_action(action)

        string = GLib.VariantType.new("s")
        add("new-connection", lambda *_: self.edit_connection(None))
        add("connect", lambda _a, v: self.open_connection(v.get_string()), string)
        add("edit-connection", lambda _a, v: self.edit_connection(v.get_string()), string)
        add("duplicate-connection", lambda _a, v: self._duplicate(v.get_string()), string)
        add("delete-connection", lambda _a, v: self._delete(v.get_string()), string)
        add("wake-connection", lambda _a, v: self._wake(v.get_string()), string)
        add("export-connection", lambda _a, v: self._export(v.get_string()), string)
        add("open-rdp", lambda *_: self._import("rdp"))
        add("import-remmina", lambda *_: self._import("remmina"))
        add("import-jump", lambda *_: self._import("jump"))
        add("settings", lambda *_: self._show_settings())
        add("shortcuts", lambda *_: self._show_shortcuts())
        add("about", lambda *_: self._show_about())
        add("quit", lambda *_: self._quit())
        add("shortcut-capture", self._on_shortcut_capture, string,
            GLib.Variant.new_string(self.settings.system_shortcut_capture))
        for action, accels in APP_ACCELS.items():
            self.set_accels_for_action(action, accels)
        for index in range(1, 10):
            self.set_accels_for_action(f"win.select-tab({index})", [f"<Control><Alt><Super>{index}"])

    def _active_window(self) -> Gtk.Window:
        return self.get_active_window() or self.main_window(create=True)

    def _on_shortcut_capture(self, action, value) -> None:
        action.set_state(value)
        self.settings.system_shortcut_capture = value.get_string()

    def edit_connection(self, connection_id: str | None) -> None:
        from .ui.editor import ConnectionEditor
        connection = self.library.connection(connection_id) if connection_id else None
        ConnectionEditor(self, connection).present(self._active_window())

    def _duplicate(self, connection_id: str) -> None:
        try:
            self.library.duplicate(connection_id)
        except Exception as error:  # noqa: BLE001
            self._error(_("Duplicating failed"), str(error))

    def _delete(self, connection_id: str) -> None:
        connection = self.library.connection(connection_id)
        if connection is None:
            return
        dialog = Adw.AlertDialog(heading=_("Delete “{name}”?").format(name=connection.name),
                                 body=_("Saved passwords are also removed from the keyring."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def answered(_d, response):
            if response == "delete":
                try:
                    self.library.delete([connection_id])
                except Exception as error:  # noqa: BLE001
                    self._error(_("Deleting failed"), str(error))

        dialog.connect("response", answered)
        dialog.present(self._active_window())

    def _wake(self, connection_id: str) -> None:
        from .store import wake_on_lan
        connection = self.library.connection(connection_id)
        if connection is None:
            return
        try:
            wake_on_lan.wake(connection.advanced.wake_on_lan_mac_addresses)
        except Exception as error:  # noqa: BLE001
            self._error(_("Wake-on-LAN failed"), str(error))
            return
        self._toast(_("Wake-up packet sent to “{name}”.").format(name=connection.name))

    def _export(self, connection_id: str) -> None:
        from .ui.importing import export_rdp_file
        connection = self.library.connection(connection_id)
        if connection is not None:
            export_rdp_file(self._active_window(), connection)

    def _import(self, kind: str) -> None:
        from .ui import importing
        window = self._active_window()
        if kind == "rdp":
            importing.choose_rdp_file(self, window)
        elif kind == "remmina":
            importing.import_remmina(self, window)
        else:
            importing.import_jump(self, window)

    def _offer_first_import(self) -> bool:
        """First start (no connections file yet): offer Remmina's connections, like the Mac app offers Jump's."""
        if self.library.is_first_start and self.library.load_error is None:
            from .ui import importing
            importing.offer_first_start_import(self, self.main_window(create=True))
        return GLib.SOURCE_REMOVE

    def _show_settings(self) -> None:
        from .ui.settings_dialog import SettingsDialog
        SettingsDialog(self).present(self._active_window())

    def _show_shortcuts(self) -> None:
        from .ui.shortcuts import shortcuts_window
        window = shortcuts_window(self._active_window())
        window.present()

    def _show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name=APP_NAME, application_icon=APP_ID, version=VERSION,
            developer_name="Marc Neuhaus", license_type=Gtk.License.APACHE_2_0,
            website="https://github.com/mneuhaus/weitblick-remote",
            comments=_("Remote Desktop for Windows PCs, made for Linux. A port of the macOS app, built on FreeRDP."),
            copyright="© 2026 Marc Neuhaus")
        about.add_credit_section(_("Built on"), ["FreeRDP https://www.freerdp.com/", "GTK https://gtk.org/",
                                                 "libadwaita https://gnome.pages.gitlab.gnome.org/libadwaita/"])
        about.present(self._active_window())

    def _quit(self) -> None:
        for window in list(self.get_windows()):
            window.close()
        self.quit()

    def _error(self, heading: str, body: str) -> None:
        from .ui.dialogs import show_message
        show_message(self._active_window(), heading, body)

    def _toast(self, message: str) -> None:
        window = self.main_window()
        if window is not None:
            window.show_toast(message)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("WEITBLICK_LOG", "WARNING").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # FreeRDP logs to stderr; keep it quiet unless asked (WLOG_LEVEL=INFO for details).
    os.environ.setdefault("WLOG_LEVEL", "WARN")
    import sys
    # WM_CLASS and the desktop file match by the app id (else it would be "__main__.py").
    GLib.set_prgname(APP_ID)
    app = WeitblickApp()
    return app.run(argv if argv is not None else sys.argv)
