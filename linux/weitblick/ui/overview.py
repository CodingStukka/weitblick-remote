"""The overview tab: saved connections with search, sorting, status of open sessions and agents
(port of OverviewView and ConnectionRow)."""

from __future__ import annotations

import time

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ..i18n import _, ngettext
from ..store.connection import ConnectionProtocol


def last_used_text(connection) -> str:
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
    if days < 60:
        return ngettext("Last used {n} day ago", "Last used {n} days ago", days).format(n=days)
    months = days // 30
    return ngettext("Last used {n} month ago", "Last used {n} months ago", months).format(n=months)


class ConnectionRow(Adw.ActionRow):
    __gtype_name__ = "WBConnectionRow"

    def __init__(self, overview: OverviewPage, connection):
        super().__init__(activatable=True, use_markup=False)
        self.overview = overview
        self.connection = connection
        self.set_title(connection.name)
        user = f"{connection.username}@" if connection.username else ""
        port = f":{connection.port}" if connection.port != 3389 else ""
        self._target = f"{user}{connection.host}{port}"
        self.add_prefix(Gtk.Image(icon_name="computer-symbolic" if connection.protocol is ConnectionProtocol.RDP
                                  else "preferences-desktop-remote-desktop-symbolic", pixel_size=24))

        self._status = Gtk.Label(css_classes=["dim-label", "caption"], visible=False)
        self._agent = Gtk.Label(css_classes=["wb-agent-badge", "caption"], visible=False)
        self._watch = Gtk.Button(label=_("Watch"), valign=Gtk.Align.CENTER, visible=False,
                                 tooltip_text=_("Open the agent’s session in a tab"))
        self._watch.connect("clicked", lambda *_: overview.app.watch_agent_session(connection.id))
        connect = Gtk.Button(label=_("Connect"), valign=Gtk.Align.CENTER)
        connect.connect("clicked", lambda *_: overview.app.open_connection(connection.id))
        menu = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                              menu_model=overview.row_menu(connection), tooltip_text=_("More"))
        for widget in (self._status, self._agent, self._watch, connect, menu):
            self.add_suffix(widget)
        self.connect("activated", lambda *_: overview.app.open_connection(connection.id))

        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", self._on_context_click)
        self.add_controller(click)
        self.update_activity()

    def _on_context_click(self, gesture, _n, x, y) -> None:
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
        self._status.set_visible(status is not None)
        if status is not None:
            self._status.set_label(status.label)
        self._agent.set_visible(agent is not None)
        self._watch.set_visible(agent is not None and agent.windowless)
        if agent is not None:
            self._agent.set_label(_("Agent paused") if agent.paused else _("Agent"))
            self._agent.set_tooltip_text(_("Controlled by “{client}”").format(client=agent.client))
        self.set_subtitle(f"{self._target} · {last_used_text(self.connection)}")


class OverviewPage(Adw.Bin):
    __gtype_name__ = "WBOverviewPage"

    def __init__(self, app):
        super().__init__()
        self.app = app
        self._rows: list[ConnectionRow] = []

        self.search = Gtk.SearchEntry(placeholder_text=_("Search connections"), hexpand=True)
        self.search.connect("search-changed", lambda *_: self.reload())
        self.search.connect("activate", self._open_first)
        self.sort = Gtk.DropDown.new_from_strings([_("By Name"), _("Recently Used First")])
        self.sort.set_tooltip_text(_("Sort Order"))
        self.sort.set_selected(1 if app.settings.overview_sort == "recent" else 0)
        self.sort.connect("notify::selected", self._sort_changed)
        bar = Gtk.Box(spacing=8)
        bar.append(self.search)
        bar.append(self.sort)
        bar_clamp = Adw.Clamp(maximum_size=960, margin_top=12, margin_bottom=6, margin_start=12, margin_end=12)
        bar_clamp.set_child(bar)

        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
                                valign=Gtk.Align.START)
        clamp = Adw.Clamp(maximum_size=960, margin_top=6, margin_bottom=24, margin_start=12, margin_end=12)
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

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(bar_clamp)
        box.append(self.stack)
        self.set_child(box)

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

    def focus_search_if_idle(self) -> None:
        if not self.search.has_focus() and self.get_root() is not None:
            focus = self.get_root().get_focus()
            if focus is None or not focus.is_ancestor(self):
                self.search.grab_focus()

    def _sort_changed(self, *_args) -> None:
        self.app.settings.overview_sort = "recent" if self.sort.get_selected() == 1 else "name"
        self.reload()

    def reload(self) -> None:
        library = self.app.library
        if getattr(library, "load_error", None):
            self.error.set_description(str(library.load_error))
            self.stack.set_visible_child_name("error")
            return
        sort = "recent" if self.sort.get_selected() == 1 else "name"
        connections = library.search(self.search.get_text(), sort)
        self.list.remove_all()
        self._rows = [ConnectionRow(self, connection) for connection in connections]
        for row in self._rows:
            self.list.append(row)
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
        for label, action in ((_("Connect"), "app.connect"), (_("Edit…"), "app.edit-connection"),
                              (_("Duplicate"), "app.duplicate-connection")):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(action, target)
            main.append_item(item)
        menu.append_section(None, main)
        extra = Gio.Menu()
        if connection.advanced.wake_on_lan_mac_addresses:
            item = Gio.MenuItem.new(_("Wake Up (Wake-on-LAN)"), None)
            item.set_action_and_target_value("app.wake-connection", target)
            extra.append_item(item)
        item = Gio.MenuItem.new(_("Export as .rdp File…"), None)
        item.set_action_and_target_value("app.export-connection", target)
        extra.append_item(item)
        menu.append_section(None, extra)
        danger = Gio.Menu()
        item = Gio.MenuItem.new(_("Delete…"), None)
        item.set_action_and_target_value("app.delete-connection", target)
        danger.append_item(item)
        menu.append_section(None, danger)
        return menu
