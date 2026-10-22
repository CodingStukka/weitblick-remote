"""The keyboard shortcuts window."""

from __future__ import annotations

from xml.sax.saxutils import escape

from gi.repository import Gtk

from ..i18n import _


def _group(title: str, items: list[tuple[str, str]]) -> str:
    shortcuts = "".join(
        f'<child><object class="GtkShortcutsShortcut"><property name="title">{escape(label)}</property>'
        f'<property name="accelerator">{escape(accel)}</property></object></child>'
        for label, accel in items)
    return f'<child><object class="GtkShortcutsGroup"><property name="title">{escape(title)}</property>{shortcuts}</object></child>'


def shortcuts_window(parent: Gtk.Window) -> Gtk.ShortcutsWindow:
    groups = [
        _group(_("Sessions (stay in Weitblick Remote)"), [
            (_("Full screen"), "<Control><Alt><Super>f"),
            (_("Next tab"), "<Control><Alt><Super>Right"),
            (_("Previous tab"), "<Control><Alt><Super>Left"),
            (_("Overview, tab 2…9"), "<Control><Alt><Super>1...9"),
            (_("Disconnect"), "<Control><Alt><Super>w"),
        ]),
        _group(_("Sent to Windows"), [
            (_("Ctrl+Alt+Del"), "<Control><Alt>End"),
        ]),
        _group(_("Overview"), [
            (_("New connection"), "<Control>n"),
            (_("Search"), "<Control>f"),
            (_("Open .rdp file"), "<Control>o"),
            (_("Settings"), "<Control>comma"),
            (_("Quit"), "<Control>q"),
        ]),
    ]
    xml = ('<interface><object class="GtkShortcutsWindow" id="window"><property name="modal">1</property>'
           '<child><object class="GtkShortcutsSection"><property name="section-name">main</property>'
           + "".join(groups) + '</object></child></object></interface>')
    builder = Gtk.Builder.new_from_string(xml, -1)
    window = builder.get_object("window")
    window.set_transient_for(parent)
    return window
