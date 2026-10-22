"""The overview tab: saved connections with search, sorting, status of open sessions and agents
(port of OverviewView and ConnectionRow)."""

from __future__ import annotations

import time

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from ..i18n import _, ngettext
from ..store.connection import ConnectionProtocol


def last_used_text(connection) -> str:
    """"Last used 3 hours ago" in steps like the macOS app's (minutes, hours, days, weeks, months, years)."""
    if connection.last_connected is None:
        return _("Never connected")
    seconds = max(0.0, time.time() - connection.last_connected_datetime.timestamp())
    if seconds < 60:
        return _("Last used just now")
    minutes = int(seconds // 60)
    if minutes < 60:
        return ngettext("Last used {n} minute ago", "Last used {n} minutes ago", minutes).format(n=minutes)
    hours = minutes // 60
    if hours < 24:
        return ngettext("Last used {n} hour ago", "Last used {n} hours ago", hours).format(n=hours)
    days = hours // 24
    if days < 7:
        return ngettext("Last used {n} day ago", "Last used {n} days ago", days).format(n=days)
    if days < 30:
        weeks = days // 7
        return ngettext("Last used {n} week ago", "Last used {n} weeks ago", weeks).format(n=weeks)
    if days < 365:
        months = max(1, days // 30)
        return ngettext("Last used {n} month ago", "Last used {n} months ago", months).format(n=months)
    years = days // 365
    return ngettext("Last used {n} year ago", "Last used {n} years ago", years).format(n=years)


def address_text(connection) -> str:
    """`DOMAIN\\user @ host:port · Screen Sharing`, leaving out what is empty or standard (as on the Mac)."""
    vnc = connection.protocol is ConnectionProtocol.VNC
    user = connection.username
    if connection.domain and user:
        user = f"{connection.domain}\\{user}"
    host = connection.host
    if connection.port != (5900 if vnc else 3389):
        host += f":{connection.port}"
    text = f"{user} @ {host}" if user else host
    if vnc:
        text += " · " + _("Screen Sharing")
    return text


STATUS_KINDS = ("connecting", "connected", "reconnecting", "disconnected")


class ConnectionRow(Gtk.ListBoxRow):
    """Icon tile with a status dot, name and address, status and last use on the right; double-click or
    Enter connects, the context menu (right-click or the … button on hover) has the rest."""

    __gtype_name__ = "WBConnectionRow"

    def __init__(self, overview: OverviewPage, connection):
        super().__init__(activatable=True, css_classes=["wb-connection-row"])
        self.overview = overview
        self.connection = connection
        vnc = connection.protocol is ConnectionProtocol.VNC

        tile = Gtk.Image(icon_name="weitblick-screen-sharing-symbolic" if vnc else "computer-symbolic", pixel_size=20,
                         css_classes=["wb-connection-icon"] + (["vnc"] if vnc else []))
        self._dot = Gtk.Box(css_classes=["wb-status-dot"], halign=Gtk.Align.END, valign=Gtk.Align.END, visible=False)
        icon = Gtk.Overlay(child=tile, valign=Gtk.Align.CENTER)
        icon.add_overlay(self._dot)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, hexpand=True)
        text.append(Gtk.Label(label=connection.name, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                              css_classes=["heading"]))
        text.append(Gtk.Label(label=address_text(connection), xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE,
                              css_classes=["dim-label"]))

        self._agent = Gtk.Label(css_classes=["wb-agent-badge", "caption"], valign=Gtk.Align.CENTER, visible=False)
        self._watch = Gtk.Button(label=_("Watch"), valign=Gtk.Align.CENTER, visible=False,
                                 tooltip_text=_("Open the agent’s session in a tab"))
        self._watch.connect("clicked", lambda *_: overview.app.watch_agent_session(connection.id))
        self._status = Gtk.Label(xalign=1, visible=False, css_classes=["caption-heading", "wb-status-label"])
        self._last_used = Gtk.Label(xalign=1, css_classes=["caption", "dim-label"])
        trailing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        trailing.append(self._status)
        trailing.append(self._last_used)
        menu = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                              css_classes=["flat", "circular", "wb-row-menu"],
                              menu_model=overview.row_menu(connection), tooltip_text=_("More"))

        body = Gtk.Box(spacing=12, hexpand=True, css_classes=["wb-row-body"])
        for widget in (text, self._agent, self._watch, trailing, menu):
            body.append(widget)
        box = Gtk.Box(spacing=14)
        box.append(icon)
        box.append(body)
        self.set_child(box)

        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", self._on_context_click)
        self.add_controller(click)
        self.update_activity()

    def _on_context_click(self, gesture, _n, x, y) -> None:
        self.overview.list.select_row(self)
        popover = Gtk.PopoverMenu.new_from_model(self.overview.row_menu(self.connection))
        popover.set_parent(self)
        popover.set_has_arrow(False)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def update_activity(self) -> None:
        activity = self.overview.app.activity
        status = activity.statuses.get(self.connection.id)
        agent = activity.agents.get(self.connection.id)
        for widget in (self._dot, self._status):
            for kind in STATUS_KINDS:
                widget.remove_css_class(kind)
            widget.set_visible(status is not None)
            if status is not None:
                widget.add_css_class(status.kind)
        if status is not None:
            self._status.set_label(status.label)
            self._dot.set_tooltip_text(status.label)
        self._agent.set_visible(agent is not None)
        self._watch.set_visible(agent is not None and agent.windowless)
        if agent is not None:
            self._agent.set_label(_("Agent paused") if agent.paused else _("Agent"))
            self._agent.set_tooltip_text(_("Controlled by “{client}”").format(client=agent.client))
        self._last_used.set_label(last_used_text(self.connection))


class OverviewPage(Adw.Bin):
    """The list of connections. Its search field and sort menu sit in the window's header bar (MainWindow)."""

    __gtype_name__ = "WBOverviewPage"

    def __init__(self, app):
        super().__init__(css_classes=["view"])
        self.app = app
        self._rows: list[ConnectionRow] = []
        self.sort_order = "recent" if app.settings.overview_sort == "recent" else "name"

        self.search = Gtk.SearchEntry(placeholder_text=_("Search"), hexpand=True)
        self.search.connect("search-changed", lambda *_: self.reload())
        self.search.connect("activate", self._open_first)
        self.search.set_key_capture_widget(self)
        search_keys = Gtk.EventControllerKey()
        search_keys.connect("key-pressed", self._on_search_key)
        self.search.add_controller(search_keys)

        self.list = Gtk.ListBox(css_classes=["wb-connection-list"], selection_mode=Gtk.SelectionMode.SINGLE,
                                activate_on_single_click=False, valign=Gtk.Align.START)
        self.list.connect("row-activated", lambda _l, row: self.app.open_connection(row.connection.id))
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_list_key)
        self.list.add_controller(keys)
        clamp = Adw.Clamp(maximum_size=1000, tightening_threshold=800, margin_top=8, margin_bottom=24)
        clamp.set_child(self.list)
        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_child(clamp)

        self.empty = Adw.StatusPage(icon_name="network-workgroup-symbolic", title=_("No Connections Yet"),
                                    description=_("Create a connection or import your connections from Remmina, "
                                                  "Jump Desktop or an .rdp file."), vexpand=True)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        for label, action, css in ((_("New Connection…"), "app.new-connection", ["pill", "suggested-action"]),
                                   (_("Import from Remmina…"), "app.import-remmina", ["pill"])):
            buttons.append(Gtk.Button(label=label, action_name=action, css_classes=css))
        self.empty.set_child(buttons)
        self.no_results = Adw.StatusPage(icon_name="edit-find-symbolic", title=_("No connections found"),
                                         vexpand=True)
        self.error = Adw.StatusPage(icon_name="dialog-error-symbolic", title=_("Can’t Read Connections"),
                                    vexpand=True)

        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(scrolled, "list")
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(self.no_results, "no-results")
        self.stack.add_named(self.error, "error")

        self.set_child(self.stack)

        shortcuts = Gtk.ShortcutController(scope=Gtk.ShortcutScope.LOCAL)
        for trigger, action in (("<Control>n", "app.new-connection"), ("<Control>q", "app.quit"),
                                ("<Control>comma", "app.settings"), ("<Control>o", "app.open-rdp")):
            shortcuts.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trigger),
                                                    Gtk.NamedAction.new(action)))
        shortcuts.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Control>f"),
                                                Gtk.CallbackAction.new(lambda *_: self.search.grab_focus() or True)))
        self.add_controller(shortcuts)

        app.library.add_listener(lambda *_: GLib.idle_add(self._reload_idle))
        app.activity.connect("changed", lambda *_: self._update_activity())
        self.reload()

    def _reload_idle(self) -> bool:
        self.reload()
        return GLib.SOURCE_REMOVE

    def do_grab_focus(self) -> bool:
        # Selecting the tab focuses the page: the search field, not the first row (that would select it).
        return self.search.grab_focus()

    def do_focus(self, direction: Gtk.DirectionType) -> bool:
        # Same when the tab view moves the focus in from a session tab. Tab from the search field still
        # enters the list.
        root = self.get_root()
        focus = root.get_focus() if root is not None else None
        if focus is None or not (focus.is_ancestor(self) or focus is self.search or focus.is_ancestor(self.search)):
            return self.search.grab_focus()
        return Adw.Bin.do_focus(self, direction)

    def focus_search_if_idle(self) -> None:
        """The search field takes the keyboard when nothing in the overview has it (Down moves into the list)."""
        root = self.get_root()
        if root is None or self.search.has_focus():
            return
        focus = root.get_focus()
        if focus is None or not (focus.is_ancestor(self) or focus.is_ancestor(self.search)):
            self.search.grab_focus()

    def _on_search_key(self, _controller, keyval: int, _keycode: int, _state: Gdk.ModifierType) -> bool:
        if keyval not in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            return False
        row = self.list.get_selected_row() or self.list.get_row_at_index(0)
        if row is None:
            return False
        self.list.select_row(row)
        row.grab_focus()
        return True

    def set_sort_order(self, order: str) -> None:
        self.sort_order = order
        self.app.settings.overview_sort = order
        self.reload()

    def _on_list_key(self, _controller, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        row = self.list.get_selected_row()
        if row is None or state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            self.activate_action("app.delete-connection", GLib.Variant.new_string(row.connection.id))
            return True
        if keyval == Gdk.KEY_Menu:
            row._on_context_click(Gtk.GestureClick(), 1, row.get_width() / 2, row.get_height() / 2)
            return True
        return False

    def reload(self) -> None:
        library = self.app.library
        if getattr(library, "load_error", None):
            self.error.set_description(str(library.load_error))
            self.stack.set_visible_child_name("error")
            return
        selected = self.list.get_selected_row()
        selected_id = selected.connection.id if selected is not None else None
        connections = library.search(self.search.get_text(), self.sort_order)
        self.list.remove_all()
        self._rows = [ConnectionRow(self, connection) for connection in connections]
        for row in self._rows:
            self.list.append(row)
            if row.connection.id == selected_id:
                self.list.select_row(row)
        if self._rows:
            self.stack.set_visible_child_name("list")
        elif library.connections:
            self.stack.set_visible_child_name("no-results")
        else:
            self.stack.set_visible_child_name("empty")

    def _update_activity(self) -> None:
        for row in self._rows:
            row.update_activity()

    def _open_first(self, *_args) -> None:
        if self._rows:
            self.app.open_connection(self._rows[0].connection.id)

    def row_menu(self, connection) -> Gio.Menu:
        target = GLib.Variant.new_string(connection.id)
        menu = Gio.Menu()
        main = Gio.Menu()
        item = Gio.MenuItem.new(_("Connect"), None)
        item.set_action_and_target_value("app.connect", target)
        main.append_item(item)
        menu.append_section(None, main)
        extra = Gio.Menu()
        for label, action in ((_("Edit…"), "app.edit-connection"), (_("Duplicate"), "app.duplicate-connection")):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(action, target)
            extra.append_item(item)
        if connection.protocol is ConnectionProtocol.RDP:
            item = Gio.MenuItem.new(_("Export as .rdp File…"), None)
            item.set_action_and_target_value("app.export-connection", target)
            extra.append_item(item)
        if connection.advanced.wake_on_lan_mac_addresses:
            item = Gio.MenuItem.new(_("Wake Up (Wake-on-LAN)"), None)
            item.set_action_and_target_value("app.wake-connection", target)
            extra.append_item(item)
        menu.append_section(None, extra)
        danger = Gio.Menu()
        item = Gio.MenuItem.new(_("Delete…"), None)
        item.set_action_and_target_value("app.delete-connection", target)
        danger.append_item(item)
        menu.append_section(None, danger)
        return menu
