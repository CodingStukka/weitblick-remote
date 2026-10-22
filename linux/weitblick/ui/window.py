"""The main window: a tab bar with the overview tab first and one tab per session, like Jump.

Tabs can be dragged out into their own windows (libadwaita does that), sessions can also open in
separate windows (settings). Keys of a selected session tab go to Windows before GTK sees them,
except Ctrl+Alt+Super shortcuts, which stay in the app.
"""

from __future__ import annotations

import logging

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ..i18n import _
from ..keyboard.session_keyboard import TextInput
from .session_page import SessionPage

log = logging.getLogger(__name__)

#: (action target, label) for the "Send keys" section of the session menu.
SEND_KEYS = [
    ("ctrl+alt+delete", "Ctrl+Alt+Del"),
    ("win", _("Windows key")),
    ("alt+tab", "Alt+Tab"),
    ("ctrl+shift+esc", _("Task Manager (Ctrl+Shift+Esc)")),
    ("printscreen", _("Print Screen")),
    ("alt+f4", "Alt+F4"),
]


class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "WBMainWindow"

    def __init__(self, app, with_overview: bool = True):
        super().__init__(application=app, default_width=1280, default_height=820)
        self.app = app
        self.set_title(_("Weitblick Remote"))
        self.set_icon_name(app.get_application_id())
        self.has_overview = with_overview
        self._focused_page: SessionPage | None = None
        self._inhibited = False
        self._page_handlers: dict[SessionPage, list[int]] = {}

        self.tab_view = Adw.TabView()
        # Shown from the first session tab on (or while a tab is dragged in), like the Mac's tab bar.
        self.tab_bar = Adw.TabBar(view=self.tab_view, autohide=False, visible=False)
        self.header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title=_("Weitblick Remote"))
        self.header.set_title_widget(self.window_title)
        self._build_header()

        self.toolbar = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.RAISED)
        self.toolbar.add_top_bar(self.header)
        self.toolbar.add_top_bar(self.tab_bar)
        self.toolbar.set_content(self.tab_view)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.toolbar)
        self.set_content(self.toasts)

        if with_overview:
            from .overview import OverviewPage
            self.overview = OverviewPage(app)
            page = self.tab_view.append_pinned(self.overview)
            page.set_title(_("Overview"))
            page.set_icon(Gio.ThemedIcon.new("view-list-symbolic"))
            self.search_title = Adw.Clamp(maximum_size=420, child=self.overview.search)
        else:
            self.overview = None
            self.search_title = None

        self.tab_view.connect("close-page", self._on_close_page)
        self.tab_view.connect("notify::selected-page", lambda *_: self._selection_changed())
        self.tab_view.connect("create-window", self._on_create_window)
        self.tab_view.connect("page-attached", self._on_page_attached)
        self.tab_view.connect("page-detached", self._on_page_detached)
        self.tab_view.connect("notify::n-pages", lambda *_: self._update_tab_bar())
        self.tab_view.connect("notify::is-transferring-page", lambda *_: self._update_tab_bar())
        self.connect("notify::is-active", lambda *_: self._selection_changed())
        self.connect("notify::fullscreened", lambda *_: self._fullscreen_changed())
        self.connect("close-request", self._on_close_request)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key, True)
        keys.connect("key-released", self._on_key, False)
        self.add_controller(keys)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

        self._install_actions()
        self._selection_changed()
        if self.overview is not None:
            # Not the first row: GTK's initial focus would select it.
            self.set_focus(self.overview.search)

    # MARK: Header

    def _build_header(self) -> None:
        self.new_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("New Connection"),
                                     action_name="app.new-connection")
        self.header.pack_start(self.new_button)

        menu = Gio.Menu()
        imports = Gio.Menu()
        imports.append(_("Open .rdp File…"), "app.open-rdp")
        imports.append(_("Import from Remmina…"), "app.import-remmina")
        imports.append(_("Import from Jump Desktop…"), "app.import-jump")
        menu.append_section(None, imports)
        app_section = Gio.Menu()
        app_section.append(_("Settings"), "app.settings")
        app_section.append(_("Keyboard Shortcuts"), "app.shortcuts")
        app_section.append(_("About Weitblick Remote"), "app.about")
        menu.append_section(None, app_section)
        quit_section = Gio.Menu()
        quit_section.append(_("Quit"), "app.quit")
        menu.append_section(None, quit_section)
        self.header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                            tooltip_text=_("Main Menu"), primary=True))

        sort_menu = Gio.Menu()
        sort_menu.append(_("By Name"), "win.sort-order::name")
        sort_menu.append(_("Recently Used First"), "win.sort-order::recent")
        self.sort_button = Gtk.MenuButton(icon_name="view-sort-descending-symbolic", menu_model=sort_menu,
                                          tooltip_text=_("Sort Order"))
        self.header.pack_end(self.sort_button)

        self.fullscreen_button = Gtk.Button(icon_name="view-fullscreen-symbolic",
                                            tooltip_text=_("Full Screen (Ctrl+Alt+Super+F)"),
                                            action_name="win.fullscreen")
        self.header.pack_end(self.fullscreen_button)

        session_menu = Gio.Menu()
        send = Gio.Menu()
        for target, label in SEND_KEYS:
            item = Gio.MenuItem.new(_("Send {keys}").format(keys=label), None)
            item.set_action_and_target_value("win.send-keys", GLib.Variant.new_string(target))
            send.append_item(item)
        session_menu.append_section(_("Keys"), send)
        text = Gio.Menu()
        for mode, label in ((TextInput.SMART, _("Smart (AltGr characters as Unicode)")),
                            (TextInput.UNICODE, _("Characters as Unicode")),
                            (TextInput.SCANCODES, _("Physical keys only"))):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value("win.text-input", GLib.Variant.new_string(mode.value))
            text.append_item(item)
        session_menu.append_section(_("Typing"), text)
        capture = Gio.Menu()
        for mode, label in (("fullscreen", _("In Full Screen")), ("always", _("Always")), ("never", _("Never"))):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value("app.shortcut-capture", GLib.Variant.new_string(mode))
            capture.append_item(item)
        session_menu.append_section(_("Pass Alt+Tab, Super… to Windows"), capture)
        other = Gio.Menu()
        other.append(_("Sync Clipboard"), "win.clipboard-sync")
        other.append(_("Reconnect"), "win.reconnect")
        other.append(_("Disconnect"), "win.close-tab")
        session_menu.append_section(None, other)
        self.session_button = Gtk.MenuButton(icon_name="input-keyboard-symbolic", menu_model=session_menu,
                                             tooltip_text=_("Session"))
        self.header.pack_end(self.session_button)
        # Clicking a header button must not take the keyboard away from the session.
        for button in (self.new_button, self.fullscreen_button, self.session_button):
            button.set_focus_on_click(False)

    # MARK: Actions

    def _install_actions(self) -> None:
        def add(name, callback, parameter=None, state=None):
            if state is not None:
                action = Gio.SimpleAction.new_stateful(name, parameter, state)
            else:
                action = Gio.SimpleAction.new(name, parameter)
            action.connect("activate", callback)
            self.add_action(action)
            return action

        add("fullscreen", lambda *_: self.toggle_fullscreen())
        add("next-tab", lambda *_: self._select_relative(1))
        add("previous-tab", lambda *_: self._select_relative(-1))
        add("select-tab", lambda _a, value: self._select_index(value.get_int32() - 1), GLib.VariantType.new("i"))
        add("close-tab", lambda *_: self._close_selected())
        self._send_keys = add("send-keys", self._on_send_keys, GLib.VariantType.new("s"))
        self._text_input = add("text-input", self._on_text_input, GLib.VariantType.new("s"),
                               GLib.Variant.new_string(TextInput.SMART.value))
        self._clipboard_sync = add("clipboard-sync", self._on_clipboard_sync, None, GLib.Variant.new_boolean(True))
        self._reconnect = add("reconnect", lambda *_: self._with_page(lambda p: p.reconnect()))
        if self.overview is not None:
            add("sort-order", self._on_sort_order, GLib.VariantType.new("s"),
                GLib.Variant.new_string(self.overview.sort_order))

    def _with_page(self, body) -> None:
        page = self.selected_session_page()
        if page is not None:
            body(page)

    def _on_send_keys(self, _action, value) -> None:
        self._with_page(lambda page: page.send_chord(value.get_string()))

    def _on_text_input(self, action, value) -> None:
        action.set_state(value)
        self._with_page(lambda page: page.set_text_input(TextInput.parse(value.get_string())))

    def _on_sort_order(self, action, value) -> None:
        action.set_state(value)
        self.overview.set_sort_order(value.get_string())

    def _on_clipboard_sync(self, action, _value) -> None:
        enabled = not action.get_state().get_boolean()
        action.set_state(GLib.Variant.new_boolean(enabled))
        self._with_page(lambda page: page.set_clipboard_enabled(enabled))

    def _update_actions(self) -> None:
        page = self.selected_session_page()
        session_selected = page is not None
        self.session_button.set_visible(session_selected)
        self.fullscreen_button.set_visible(session_selected)
        self.new_button.set_visible(not session_selected and self.has_overview)
        self.sort_button.set_visible(not session_selected and self.has_overview)
        if page is None:
            # The overview: its search field takes the title's place.
            if self.search_title is not None:
                self._set_title_widget(self.search_title)
            else:
                self.window_title.set_title(_("Weitblick Remote"))
                self.window_title.set_subtitle("")
            for action in (self._send_keys, self._text_input, self._clipboard_sync, self._reconnect):
                action.set_enabled(False)
            return
        self._set_title_widget(self.window_title)
        self.window_title.set_title(page.title)
        self.window_title.set_subtitle("" if page.status.kind == "connected" else page.status.label)
        live = page.is_connected and page.accepts_local_input
        self._send_keys.set_enabled(live)
        self._text_input.set_enabled(True)
        self._text_input.set_state(GLib.Variant.new_string(page.text_input.value))
        self._clipboard_sync.set_enabled(page.connection.redirection.clipboard and not page.is_agent_session)
        self._clipboard_sync.set_state(GLib.Variant.new_boolean(page.clipboard_enabled))
        self._reconnect.set_enabled(page.can_reconnect)

    def _set_title_widget(self, widget: Gtk.Widget) -> None:
        if self.header.get_title_widget() is not widget:
            self.header.set_title_widget(widget)

    def _update_tab_bar(self) -> None:
        self.tab_bar.set_visible(self.tab_view.get_n_pages() > 1 or self.tab_view.get_is_transferring_page())

    def show_toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=message, timeout=4))

    # MARK: Tabs

    def selected_session_page(self) -> SessionPage | None:
        page = self.tab_view.get_selected_page()
        child = page.get_child() if page else None
        return child if isinstance(child, SessionPage) else None

    def session_pages(self) -> list[SessionPage]:
        pages = []
        for index in range(self.tab_view.get_n_pages()):
            child = self.tab_view.get_nth_page(index).get_child()
            if isinstance(child, SessionPage):
                pages.append(child)
        return pages

    def add_session(self, page: SessionPage, select: bool = True) -> None:
        tab = self.tab_view.append(page)
        tab.set_title(page.title)
        if select:
            self.tab_view.set_selected_page(tab)

    def show_page(self, page: SessionPage) -> None:
        tab = self.tab_view.get_page(page)
        if tab is not None:
            self.tab_view.set_selected_page(tab)
        self.present()

    def show_overview(self) -> None:
        if self.overview is not None:
            self.tab_view.set_selected_page(self.tab_view.get_page(self.overview))
        self.present()

    def _select_relative(self, delta: int) -> None:
        count = self.tab_view.get_n_pages()
        if count < 2:
            return
        current = self.tab_view.get_page_position(self.tab_view.get_selected_page())
        self.tab_view.set_selected_page(self.tab_view.get_nth_page((current + delta) % count))

    def _select_index(self, index: int) -> None:
        if 0 <= index < self.tab_view.get_n_pages():
            self.tab_view.set_selected_page(self.tab_view.get_nth_page(index))

    def _close_selected(self) -> None:
        page = self.tab_view.get_selected_page()
        if page is not None and isinstance(page.get_child(), SessionPage):
            self.tab_view.close_page(page)

    def _on_close_page(self, view: Adw.TabView, tab: Adw.TabPage) -> bool:
        child = tab.get_child()
        if not isinstance(child, SessionPage):
            view.close_page_finish(tab, False)
            return Gdk.EVENT_STOP

        def answered(confirmed: bool) -> None:
            view.close_page_finish(tab, confirmed)
            if confirmed:
                child.close()

        child.request_close(answered)
        return Gdk.EVENT_STOP

    def _on_create_window(self, _view) -> Adw.TabView:
        window = MainWindow(self.app, with_overview=False)
        window.present()
        return window.tab_view

    def _on_page_attached(self, _view, tab: Adw.TabPage, _position: int) -> None:
        child = tab.get_child()
        if not isinstance(child, SessionPage):
            return
        child.was_presented = True
        self._page_handlers[child] = [
            child.connect("status-changed", lambda page: self._page_status_changed(page)),
            child.connect("close-requested", lambda page: self._close_page_of(page)),
            child.connect("input-state-changed", lambda *_: self._selection_changed()),
            child.connect("agent-control-changed", lambda page: self._page_status_changed(page)),
        ]
        self._page_status_changed(child)
        child.publish_agent_activity()

    def _on_page_detached(self, view: Adw.TabView, tab: Adw.TabPage, _position: int) -> None:
        child = tab.get_child()
        if isinstance(child, SessionPage):
            for handler in self._page_handlers.pop(child, []):
                child.disconnect(handler)
            if child is self._focused_page:
                child.focus_lost()
                self._focused_page = None
        # A window of dragged-out sessions closes with its last tab.
        if not self.has_overview and view.get_n_pages() == 0:
            GLib.idle_add(self.destroy)

    def _close_page_of(self, page: SessionPage) -> None:
        tab = self.tab_view.get_page(page)
        if tab is not None:
            self.tab_view.close_page(tab)

    def _page_status_changed(self, page: SessionPage) -> None:
        tab = self.tab_view.get_page(page)
        if tab is None:
            return
        status = page.status
        tab.set_loading(status.kind in ("connecting", "reconnecting"))
        if page.agent_control is not None:
            tab.set_indicator_icon(Gio.ThemedIcon.new("weitblick-agent-symbolic"))
            tab.set_indicator_tooltip(_("Agent: {client}").format(client=page.agent_control.client))
        elif status.kind == "disconnected":
            tab.set_indicator_icon(Gio.ThemedIcon.new("network-offline-symbolic"))
            tab.set_indicator_tooltip(status.label)
        else:
            tab.set_indicator_icon(None)
        tab.set_tooltip(f"{page.connection.name} – {status.label}")
        if page is self.selected_session_page():
            self._update_actions()

    # MARK: Focus, keyboard, system shortcuts

    def _selection_changed(self) -> None:
        page = self.selected_session_page()
        active = self.is_active()
        focused = page if active else None
        if focused is not self._focused_page:
            if self._focused_page is not None:
                self._focused_page.focus_lost()
            self._focused_page = focused
            if focused is not None:
                focused.focus_gained()
        # Ctrl+Tab, Ctrl+PageUp … belong to Windows while a session tab is selected.
        self.tab_view.set_shortcuts(Adw.TabViewShortcuts.NONE if page is not None
                                    else Adw.TabViewShortcuts.ALL_SHORTCUTS)
        if page is None and self.overview is not None and active:
            self.overview.focus_search_if_idle()
        self._update_actions()
        self.update_shortcut_capture()

    def update_shortcut_capture(self) -> None:
        page = self.selected_session_page()
        wanted = page is not None and page.wants_system_shortcuts(self.is_active(), self.is_fullscreen())
        surface = self.get_surface()
        if surface is None or wanted == self._inhibited:
            return
        self._inhibited = wanted
        if wanted:
            surface.inhibit_system_shortcuts(None)
        else:
            surface.restore_system_shortcuts()

    def _on_key(self, controller: Gtk.EventControllerKey, keyval: int, keycode: int, state: Gdk.ModifierType,
                pressed: bool) -> bool:
        page = self.selected_session_page()
        if page is None:
            return False
        # Dialogs, menus and the tab bar keep their keys; only the session view forwards them.
        focus = self.get_focus()
        if focus is None or not (focus is page or focus.is_ancestor(page)):
            return False
        event = controller.get_current_event()
        level = event.get_level() if event is not None else 0
        return page.handle_key(keycode, keyval, state, level, pressed)

    # MARK: Full screen

    def toggle_fullscreen(self) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _fullscreen_changed(self) -> None:
        full = self.is_fullscreen()
        self.toolbar.set_reveal_top_bars(not full)
        self.fullscreen_button.set_icon_name("view-restore-symbolic" if full else "view-fullscreen-symbolic")
        self.update_shortcut_capture()

    def _on_motion(self, _controller, _x: float, y: float) -> None:
        # In full screen the tab bar shows while the pointer is at the top edge.
        if not self.is_fullscreen():
            return
        if y <= 2 and not self.toolbar.get_reveal_top_bars():
            self.toolbar.set_reveal_top_bars(True)
        elif y > self.toolbar.get_top_bar_height() + 24 and self.toolbar.get_reveal_top_bars():
            self.toolbar.set_reveal_top_bars(False)

    # MARK: Closing

    def _on_close_request(self, _window) -> bool:
        pages = self.session_pages()
        if not pages:
            return False
        if self.has_overview and self.app.background_mode_wanted():
            # Agent control keeps the app running without a window; sessions stay open.
            pass
        live = [page for page in pages if page.live is not None]
        if not live or not self.app.settings.confirm_close_session:
            self._close_all(pages)
            return False
        dialog = Adw.AlertDialog(
            heading=_("Disconnect all sessions?"),
            body=_("{n} sessions are open in this window. Open programs keep running in Windows.").format(n=len(live)))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("close", _("Disconnect All"))
        dialog.set_response_appearance("close", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def answered(_d, response: str) -> None:
            if response == "close":
                self._close_all(self.session_pages())
                self.destroy()

        dialog.connect("response", answered)
        dialog.present(self)
        return True

    def _close_all(self, pages: list[SessionPage]) -> None:
        for page in pages:
            page.close()
