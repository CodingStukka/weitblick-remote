"""The connection editor: General, Display, Keyboard, Devices, Security and Advanced pages over a
ConnectionDraft; nothing is stored until Save (port of ConnectionEditorView and its pages)."""

from __future__ import annotations

import logging
import os
from typing import Callable

from gi.repository import Adw, GLib, Gtk

from ..i18n import _
from ..keyboard import layouts
from ..store import AgentAccess, AudioPlaybackMode, ConnectionDraft, ConnectionProtocol, DriveMapping, TextInput

log = logging.getLogger(__name__)

SCALES = [100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500]


def _combo(title: str, labels: list[str], selected: int, on_change: Callable[[int], None],
           subtitle: str = "") -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, subtitle=subtitle, model=Gtk.StringList.new(labels))
    row.set_selected(max(selected, 0))
    row.connect("notify::selected", lambda r, _p: on_change(r.get_selected()))
    return row


def _switch(title: str, active: bool, on_change: Callable[[bool], None], subtitle: str = "") -> Adw.SwitchRow:
    row = Adw.SwitchRow(title=title, subtitle=subtitle, active=active)
    row.connect("notify::active", lambda r, _p: on_change(r.get_active()))
    return row


def _entry(title: str, text: str | None, on_change: Callable[[str], None]) -> Adw.EntryRow:
    row = Adw.EntryRow(title=title, text=text or "")
    row.connect("changed", lambda r: on_change(r.get_text()))
    return row


def _spin(title: str, value: int, lower: int, upper: int, on_change: Callable[[int], None]) -> Adw.SpinRow:
    row = Adw.SpinRow.new_with_range(lower, upper, 1)
    row.set_title(title)
    row.set_value(value)
    row.connect("notify::value", lambda r, _p: on_change(int(r.get_value())))
    return row


class ConnectionEditor(Adw.Dialog):
    __gtype_name__ = "WBConnectionEditor"

    def __init__(self, app, connection=None):
        super().__init__(content_width=760, content_height=700)
        self.app = app
        library = app.library
        if connection is None:
            self.draft = ConnectionDraft.new()
            self.set_title(_("New Connection"))
        else:
            self.draft = ConnectionDraft.editing(connection, library.password(connection.id) is not None)
            self.set_title(connection.name)
        c = self.draft.connection
        # Rows report changes while they are built: the banner and Save exist first.
        self.problem = Adw.Banner()
        self.save_button = Gtk.Button(label=_("Save"), css_classes=["suggested-action"])
        self.save_button.connect("clicked", self._save)

        self.stack = Adw.ViewStack()
        self._add_page("general", _("General"), "preferences-system-symbolic", self._general_page())
        self._add_page("display", _("Display"), "video-display-symbolic", self._display_page())
        self._add_page("keyboard", _("Keyboard"), "input-keyboard-symbolic", self._keyboard_page())
        self._add_page("devices", _("Devices"), "folder-remote-symbolic", self._devices_page())
        self._add_page("security", _("Security"), "security-high-symbolic", self._security_page())
        self._add_page("advanced", _("Advanced"), "applications-engineering-symbolic", self._advanced_page())

        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title=self.get_title()))
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE, halign=Gtk.Align.CENTER,
                                    margin_bottom=6)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        header.pack_end(self.save_button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(switcher)
        toolbar.add_top_bar(self.problem)
        toolbar.set_content(self.stack)
        self.set_child(toolbar)
        self.set_default_widget(self.save_button)
        self._update_problem()
        self._protocol_changed()
        self.set_focus(self._host_row if not c.host else self._name_row)

    def _add_page(self, name: str, title: str, icon: str, page: Adw.PreferencesPage) -> None:
        self.stack.add_titled_with_icon(page, name, title, icon)

    def _changed(self, *_args) -> None:
        self._update_problem()

    def _update_problem(self) -> None:
        problem = self.draft.problem
        # A new, still empty draft shows no complaint yet; Save just stays off.
        empty_new = self.draft.is_new and not self.draft.connection.host.strip()
        self.problem.set_title(problem or "")
        self.problem.set_revealed(problem is not None and not empty_new)
        self.save_button.set_sensitive(problem is None)

    # MARK: General

    def _general_page(self) -> Adw.PreferencesPage:
        c = self.draft.connection
        page = Adw.PreferencesPage()
        main = Adw.PreferencesGroup()

        def set_name(text: str) -> None:
            c.name = text
        self._name_row = _entry(_("Name"), c.name, set_name)
        main.add(self._name_row)

        def set_protocol(index: int) -> None:
            new = ConnectionProtocol.VNC if index == 1 else ConnectionProtocol.RDP
            if new is ConnectionProtocol.VNC and c.port == 3389:
                self._port_row.set_value(5900)
            elif new is ConnectionProtocol.RDP and c.port == 5900:
                self._port_row.set_value(3389)
            c.protocol = new
            self._protocol_changed()
        main.add(_combo(_("Type"), [_("Remote Desktop (RDP)"), _("Screen Sharing (VNC)")],
                        1 if c.protocol is ConnectionProtocol.VNC else 0, set_protocol))

        def set_host(text: str) -> None:
            c.host = text
            self._changed()
        self._host_row = _entry(_("Host (computer name or IP address)"), c.host, set_host)
        main.add(self._host_row)

        def set_port(value: int) -> None:
            c.port = value
            self._changed()
        self._port_row = _spin(_("Port"), c.port, 1, 65535, set_port)
        main.add(self._port_row)
        page.add(main)

        self._sign_in = Adw.PreferencesGroup(title=_("Sign-in"))
        self._sign_in.add(_entry(_("User"), c.username, lambda text: setattr(c, "username", text)))
        self._sign_in.add(_entry(_("Domain (optional)"), c.domain, lambda text: setattr(c, "domain", text)))
        password = Adw.PasswordEntryRow(
            title=_("Password (saved – type to replace)") if self.draft.has_stored_password else _("Password"))
        password.connect("changed", lambda r: setattr(self.draft, "password", r.get_text()))
        self._sign_in.add(password)
        if self.draft.has_stored_password:
            self._sign_in.add(_switch(_("Delete saved password"), False,
                                      lambda on: setattr(self.draft, "remove_stored_password", on)))
        page.add(self._sign_in)

        self._vnc_note = Adw.PreferencesGroup(
            description=_("Opens in your VNC app (a vnc:// link, e.g. Remmina or GNOME Connections), which handles "
                          "sign-in and settings itself."))
        page.add(self._vnc_note)

        notes = Adw.PreferencesGroup(title=_("Notes"))
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=10, bottom_margin=10, left_margin=12,
                            right_margin=12, height_request=80, css_classes=["card"])
        view.get_buffer().set_text(c.notes)
        view.get_buffer().connect("changed", lambda b: setattr(c, "notes", b.get_text(b.get_start_iter(),
                                                                                      b.get_end_iter(), False)))
        notes.add(view)
        page.add(notes)
        return page

    def _protocol_changed(self) -> None:
        rdp = self.draft.connection.protocol is ConnectionProtocol.RDP
        self._sign_in.set_visible(rdp)
        self._vnc_note.set_visible(not rdp)
        for name in ("display", "keyboard", "devices", "security", "advanced"):
            self.stack.get_page(self.stack.get_child_by_name(name)).set_visible(rdp)

    # MARK: Display

    def _display_page(self) -> Adw.PreferencesPage:
        d = self.draft.connection.display
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        dynamic = _switch(_("Resize with the window"), d.dynamic_resolution,
                          lambda on: setattr(d, "dynamic_resolution", on))
        width = _spin(_("Width"), d.fixed_width, 200, 8192, lambda v: (setattr(d, "fixed_width", v), self._changed()))
        height = _spin(_("Height"), d.fixed_height, 200, 8192,
                       lambda v: (setattr(d, "fixed_height", v), self._changed()))

        def set_mode(index: int) -> None:
            d.match_screen_resolution = index == 0
            dynamic.set_visible(d.match_screen_resolution)
            width.set_visible(not d.match_screen_resolution)
            height.set_visible(not d.match_screen_resolution)
            self._changed()
        group.add(_combo(_("Resolution"), [_("Match window size"), _("Fixed size")],
                         0 if d.match_screen_resolution else 1, set_mode))
        for row in (dynamic, width, height):
            group.add(row)
        set_mode(0 if d.match_screen_resolution else 1)
        page.add(group)

        quality = Adw.PreferencesGroup()
        quality.add(_switch(_("HiDPI (full resolution)"), d.retina, lambda on: setattr(d, "retina", on),
                            _("One Windows pixel per screen pixel, sharp on HiDPI screens. Off: Windows draws at "
                              "the logical size and the picture is scaled up (softer, less data).")))
        labels = [_("Automatic")] + [f"{scale} %" for scale in SCALES]
        current = SCALES.index(d.desktop_scale_factor) + 1 if d.desktop_scale_factor in SCALES else 0

        def set_scale(index: int) -> None:
            d.desktop_scale_factor = SCALES[index - 1] if index > 0 else None
        quality.add(_combo(_("Scaling in Windows"), labels, current, set_scale))
        quality.add(_switch(_("Start in full screen"), d.start_fullscreen, lambda on: setattr(d, "start_fullscreen", on)))
        page.add(quality)
        return page

    # MARK: Keyboard

    def _keyboard_page(self) -> Adw.PreferencesPage:
        k = self.draft.connection.keyboard
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            description=_("Keys go to Windows as on a PC keyboard. Shortcuts with Ctrl+Alt+Super stay in Weitblick "
                          "Remote; Ctrl+Alt+End sends Ctrl+Alt+Del."))
        modes = [TextInput.SMART, TextInput.UNICODE, TextInput.SCANCODES]
        group.add(_combo(
            _("Typing"),
            [_("Smart (AltGr characters as Unicode)"), _("Characters as Unicode"), _("Physical keys only")],
            modes.index(k.text_input) if k.text_input in modes else 0,
            lambda index: setattr(k, "text_input", modes[index]),
            _("Smart sends keys, but characters typed with AltGr arrive as shown on this computer’s layout. "
              "Unicode helps when the layouts differ; games and some shortcuts then don’t work.")))
        known = layouts.known_layouts()
        local = layouts.layout_name(layouts.current_klid())
        labels = [_("Automatic (same as this computer: {name})").format(name=local)] + [layout.name for layout in known]
        ids = [None] + [layout.klid for layout in known]
        current = ids.index(k.layout_override) if k.layout_override in ids else 0
        group.add(_combo(_("Keyboard layout in Windows"), labels, current,
                         lambda index: setattr(k, "layout_override", ids[index])))
        page.add(group)
        return page

    # MARK: Devices

    def _devices_page(self) -> Adw.PreferencesPage:
        r = self.draft.connection.redirection
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        group.add(_switch(_("Clipboard"), r.clipboard, lambda on: setattr(r, "clipboard", on),
                          _("Text, images and files in both directions")))
        sounds = [AudioPlaybackMode.LOCAL, AudioPlaybackMode.REMOTE, AudioPlaybackMode.OFF]
        group.add(_combo(_("Sound"), [_("Play on this computer"), _("Leave on the remote PC"), _("Off")],
                         sounds.index(r.audio_playback) if r.audio_playback in sounds else 0,
                         lambda index: setattr(r, "audio_playback", sounds[index])))
        group.add(_switch(_("Microphone"), r.microphone, lambda on: setattr(r, "microphone", on)))
        group.add(_switch(_("Printers"), r.printers, lambda on: setattr(r, "printers", on),
                          _("Local printers appear in Windows (needs the “Microsoft Print to PDF” driver there)")))
        page.add(group)

        self._drives = Adw.PreferencesGroup(title=_("Drives"),
                                            description=_("Appear in Windows as \\\\tsclient\\<name>."))
        add = Gtk.Button(label=_("Add Folder…"), css_classes=["flat"], valign=Gtk.Align.CENTER)
        add.connect("clicked", self._add_drive)
        self._drives.set_header_suffix(add)
        self._drives.add(_switch(_("Share folders as drives"), r.drive_redirection,
                                 lambda on: setattr(r, "drive_redirection", on)))
        self._drive_rows: list[Gtk.Widget] = []
        self._reload_drives()
        page.add(self._drives)
        return page

    def _reload_drives(self) -> None:
        r = self.draft.connection.redirection
        for row in self._drive_rows:
            self._drives.remove(row)
        self._drive_rows = []
        for drive in r.drives:
            row = Adw.EntryRow(title=drive.local_path, text=drive.name)
            row.connect("changed", lambda entry, d=drive: setattr(d, "name", entry.get_text().strip()))
            if drive.read_only:
                row.set_tooltip_text(_("Read-only: not shared (sharing needs write access)."))
            enabled = Gtk.Switch(active=drive.enabled and not drive.read_only, valign=Gtk.Align.CENTER,
                                 sensitive=not drive.read_only)
            enabled.connect("notify::active", lambda s, _p, d=drive: setattr(d, "enabled", s.get_active()))
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                tooltip_text=_("Remove"))
            remove.connect("clicked", lambda _b, d=drive: self._remove_drive(d))
            row.add_suffix(enabled)
            row.add_suffix(remove)
            self._drives.add(row)
            self._drive_rows.append(row)

    def _add_drive(self, *_args) -> None:
        dialog = Gtk.FileDialog(title=_("Share a Folder"))

        def chosen(dialog, result) -> None:
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            path = folder.get_path() if folder else None
            if not path:
                return
            home = os.path.expanduser("~")
            shown = "~" + path[len(home):] if path == home or path.startswith(home + "/") else path
            name = os.path.basename(path.rstrip("/")) or "root"
            r = self.draft.connection.redirection
            r.drives.append(DriveMapping(name=name, local_path=shown))
            r.drive_redirection = True
            self._reload_drives()

        dialog.select_folder(self.get_root() if isinstance(self.get_root(), Gtk.Window) else None, None, chosen)

    def _remove_drive(self, drive) -> None:
        self.draft.connection.redirection.drives.remove(drive)
        self._reload_drives()

    # MARK: Security

    def _security_page(self) -> Adw.PreferencesPage:
        s = self.draft.connection.security
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        group.add(_switch(_("Network Level Authentication (NLA)"), not s.disable_nla,
                          lambda on: setattr(s, "disable_nla", not on),
                          _("Signs in before the connection is set up. Turn off only for very old Windows versions.")))
        group.add(_switch(_("Don’t check the certificate"), s.ignore_certificate_errors,
                          lambda on: setattr(s, "ignore_certificate_errors", on),
                          _("Never asks about the certificate. Only on trusted networks.")))
        group.add(_switch(_("Console session"), s.console_session, lambda on: setattr(s, "console_session", on),
                          _("Connects to the console like “mstsc /admin”")))
        page.add(group)

        self._certificates = Adw.PreferencesGroup(title=_("Trusted certificates (SHA-256)"))
        self._certificate_rows: list[Gtk.Widget] = []
        self._reload_certificates()
        page.add(self._certificates)

        agents = Adw.PreferencesGroup(
            description=_("Whether AI agents may control this connection over MCP (Settings → AI Agents). “Ask” "
                          "shows a prompt each time an agent wants it."))
        choices = [AgentAccess.ASK, AgentAccess.ALWAYS, AgentAccess.NEVER]
        agents.add(_combo(_("AI agent access"), [_("Ask"), _("Always allow"), _("Never")],
                          choices.index(s.agent_access) if s.agent_access in choices else 0,
                          lambda index: setattr(s, "agent_access", choices[index])))
        page.add(agents)
        return page

    def _reload_certificates(self) -> None:
        s = self.draft.connection.security
        for row in self._certificate_rows:
            self._certificates.remove(row)
        self._certificate_rows = []
        if not s.trusted_certificate_fingerprints:
            row = Adw.ActionRow(title=_("None yet. You’ll be asked on the first connection."))
            row.add_css_class("dim-label")
            self._certificates.add(row)
            self._certificate_rows.append(row)
            return
        for fingerprint in s.trusted_certificate_fingerprints:
            row = Adw.ActionRow(title=fingerprint, title_selectable=True, css_classes=["monospace"])
            row.set_title_lines(2)
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                tooltip_text=_("Forget"))
            remove.connect("clicked", lambda _b, f=fingerprint: self._forget_certificate(f))
            row.add_suffix(remove)
            self._certificates.add(row)
            self._certificate_rows.append(row)

    def _forget_certificate(self, fingerprint: str) -> None:
        self.draft.connection.security.trusted_certificate_fingerprints.remove(fingerprint)
        self._reload_certificates()

    # MARK: Advanced

    def _advanced_page(self) -> Adw.PreferencesPage:
        a = self.draft.connection.advanced
        page = Adw.PreferencesPage()
        shell = Adw.PreferencesGroup(title=_("Program instead of desktop"))
        shell.add(_entry(_("Program, e.g. C:\\Program Files\\App\\app.exe"), a.alternate_shell,
                         lambda text: setattr(a, "alternate_shell", text)))
        shell.add(_entry(_("Working directory (optional)"), a.working_dir, lambda text: setattr(a, "working_dir", text)))
        page.add(shell)
        other = Adw.PreferencesGroup()
        other.add(_entry(_("Load balance info (optional)"), a.load_balance_info,
                         lambda text: setattr(a, "load_balance_info", text)))
        other.add(_entry(_("Wake-on-LAN: MAC addresses, separated by commas"),
                         ", ".join(a.wake_on_lan_mac_addresses),
                         lambda text: setattr(a, "wake_on_lan_mac_addresses",
                                              [part for part in text.replace(";", ",").split(",")])))
        if a.gateway_hostname or a.gateway_ref:
            other.add(Adw.ActionRow(title=_("Gateway"),
                                    subtitle=_("{host} (not supported yet)").format(
                                        host=a.gateway_hostname or _("imported"))))
        page.add(other)
        return page

    # MARK: Saving

    def _save(self, *_args) -> None:
        if self.draft.problem is not None:
            return
        library = self.app.library
        saved = self.draft.saved()
        try:
            if self.draft.is_new:
                library.add(saved)
            else:
                library.update(saved)
            change = self.draft.password_change
            if change.kind == "set":
                library.set_password(change.password, saved.id)
            elif change.kind == "remove":
                library.delete_password(saved.id)
        except Exception as error:  # noqa: BLE001
            log.exception("saving the connection failed")
            dialog = Adw.AlertDialog(heading=_("Connection not saved"), body=str(error))
            dialog.add_response("ok", _("OK"))
            dialog.present(self)
            return
        self.close()
