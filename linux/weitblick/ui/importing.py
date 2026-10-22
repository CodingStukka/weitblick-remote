"""Imports and exports: .rdp files, Remmina and Jump Desktop with a preview (port of
JumpImportView/JumpImportFlow and the overview's .rdp handling)."""

from __future__ import annotations

import logging
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from ..i18n import _, ngettext
from ..store import ImportAction, ImportPlan, jump, rdpfile, remmina
from ..store.connection import ConnectionProtocol, new_id

log = logging.getLogger(__name__)


# MARK: .rdp files

def choose_rdp_file(app, window: Gtk.Window) -> None:
    dialog = Gtk.FileDialog(title=_("Open .rdp File"))
    rdp_filter = Gtk.FileFilter(name=_("Remote Desktop files"))
    rdp_filter.add_pattern("*.rdp")
    rdp_filter.add_pattern("*.RDP")
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(rdp_filter)
    dialog.set_filters(filters)

    def chosen(dialog, result) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        for index in range(files.get_n_items()):
            path = files.get_item(index).get_path()
            if path:
                open_rdp_file(app, window, Path(path))

    dialog.open_multiple(window, None, chosen)


def open_rdp_file(app, window: Gtk.Window, path: Path) -> None:
    """An .rdp file from the file manager or the open dialog: imported, never connected right away."""
    try:
        result = rdpfile.read(path)
    except Exception as error:  # noqa: BLE001
        _alert(window, _("Import failed"), _("{file}: couldn’t be imported ({error}).").format(
            file=path.name, error=error))
        return
    connection = result.connection
    connection.id = new_id()
    try:
        app.library.add(connection)
    except Exception as error:  # noqa: BLE001
        _alert(window, _("Import failed"), str(error))
        return
    notes = [f"{issue.field}: {issue.reason}" for issue in result.ignored_fields] + list(result.warnings)
    if notes:
        _alert(window, _("Imported “{name}” with notes").format(name=connection.name),
               _("Not imported:") + "\n" + "\n".join(notes))
    elif hasattr(window, "show_toast"):
        window.show_toast(_("Imported “{name}”.").format(name=connection.name))


def export_rdp_file(window: Gtk.Window, connection) -> None:
    dialog = Gtk.FileDialog(title=_("Export as .rdp File"), initial_name=f"{connection.name}.rdp")

    def chosen(dialog, result) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        try:
            Path(file.get_path()).write_bytes(rdpfile.encode(connection))
        except Exception as error:  # noqa: BLE001
            _alert(window, _("Export failed"), str(error))

    dialog.save(window, None, chosen)


# MARK: Remmina and Jump

def import_remmina(app, window: Gtk.Window, folder: Path | None = None) -> None:
    folders = [folder] if folder else remmina.default_directories()
    if not folders:
        _choose_folder(window, _("Choose Remmina’s Folder"), lambda path: import_remmina(app, window, path))
        return
    try:
        plan = remmina.RemminaImporter(folders[0]).plan(app.library.connections)
    except OSError as error:
        _alert(window, _("Can’t read Remmina connections"), str(error))
        return
    ImportDialog(app, plan, "Remmina", remmina.password_notice()).present(window)


def import_jump(app, window: Gtk.Window, folder: Path | None = None) -> None:
    if folder is None:
        _choose_folder(window, _("Choose the folder with the .jump files (copied from the Mac)"),
                       lambda path: import_jump(app, window, path))
        return
    try:
        plan = jump.JumpImporter(folder).plan(app.library.connections)
    except OSError as error:
        _alert(window, _("Can’t read Jump connections"), str(error))
        return
    ImportDialog(app, plan, "Jump Desktop", jump.password_notice()).present(window)


def offer_first_start_import(app, window: Gtk.Window) -> None:
    """First start: if Remmina has RDP connections, offer them (the Mac app offers Jump's)."""
    folders = remmina.default_directories()
    if not folders:
        return
    try:
        plan = remmina.RemminaImporter(folders[0]).plan(app.library.connections)
    except OSError:
        return
    if plan.create_count == 0:
        return
    ImportDialog(app, plan, "Remmina", remmina.password_notice()).present(window)


def _choose_folder(window: Gtk.Window, title: str, then) -> None:
    dialog = Gtk.FileDialog(title=title)

    def chosen(dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder and folder.get_path():
            then(Path(folder.get_path()))

    dialog.select_folder(window, None, chosen)


def _alert(parent: Gtk.Widget, heading: str, body: str) -> None:
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("ok", _("OK"))
    dialog.present(parent)


def _import_address(connection) -> str:
    text = f"{connection.host}:{connection.port}"
    if connection.protocol is ConnectionProtocol.VNC:
        text += " · " + _("Screen Sharing")
    return text


def _badge(action: ImportAction) -> Gtk.Label:
    """"New" in green, "Update" in the accent color, "Unchanged" grey, as pills like the Mac's."""
    label, kind = {ImportAction.CREATE: (_("New"), "new"), ImportAction.UPDATE: (_("Update"), "update"),
                   ImportAction.UNCHANGED: (_("Unchanged"), "unchanged")}[action]
    return Gtk.Label(label=label, valign=Gtk.Align.CENTER, css_classes=["wb-import-badge", kind, "caption-heading"])


class ImportDialog(Adw.Dialog):
    """Preview with check boxes, then the import. The source files are only read."""

    __gtype_name__ = "WBImportDialog"

    def __init__(self, app, plan: ImportPlan, source: str, password_notice: str):
        super().__init__(title=_("Import from {source}").format(source=source), content_width=620,
                         content_height=600)
        self.app = app
        self.plan = plan
        self._checks: dict[str, Gtk.CheckButton] = {}

        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False, show_title=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.import_button = Gtk.Button(css_classes=["suggested-action"])
        self.import_button.connect("clicked", self._apply)
        header.pack_end(self.import_button)

        page = Adw.PreferencesPage()
        parts = []
        if plan.create_count:
            parts.append(_("{n} new").format(n=plan.create_count))
        if plan.update_count:
            parts.append(_("{n} changed").format(n=plan.update_count))
        if plan.unchanged_count:
            parts.append(_("{n} unchanged").format(n=plan.unchanged_count))
        skipped = [item for item in plan.report.connections if item.action is None]
        if skipped:
            parts.append(_("{n} not imported").format(n=len(skipped)))
        counts = ", ".join(parts) if parts else _("No connections found")
        intro = Gtk.Box(spacing=16)
        intro.append(Gtk.Image(icon_name="folder-download-symbolic", pixel_size=40, valign=Gtk.Align.CENTER,
                               css_classes=["dim-label"]))
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        texts.append(Gtk.Label(label=_("Import connections from {source}").format(source=source), xalign=0,
                               wrap=True, css_classes=["title-3"]))
        texts.append(Gtk.Label(label=_("{counts}. {source}’s files are only read, never changed.").format(
            counts=counts, source=source), xalign=0, wrap=True, css_classes=["dim-label"]))
        intro.append(texts)
        summary = Adw.PreferencesGroup()
        summary.add(intro)
        page.add(summary)

        reports = {item.unique_id: item for item in plan.report.connections if item.unique_id}
        group = Adw.PreferencesGroup(title=_("Connections"))
        notes: list[str] = []
        for entry in plan.entries:
            connection = entry.connection
            check = Gtk.CheckButton(active=entry.action is not ImportAction.UNCHANGED,
                                    sensitive=entry.action is not ImportAction.UNCHANGED, valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda *_: self._update_button())
            self._checks[entry.id] = check
            row = Adw.ActionRow(title=connection.name, subtitle=_import_address(connection), activatable_widget=check)
            row.add_prefix(check)
            row.add_suffix(_badge(entry.action))
            group.add(row)
            report = reports.get(connection.import_source.unique_id if connection.import_source else None)
            if report is not None:
                own = [f"{issue.field}: {issue.reason}" for issue in report.ignored_fields]
                own += [warning for warning in report.warnings if warning != password_notice]
                notes += [f"{connection.name} – {note}" for note in own]
        page.add(group)

        if skipped:
            unread = Adw.PreferencesGroup(title=_("Not imported"))
            for item in skipped:
                reason = "; ".join(item.warnings) or ", ".join(f"{i.field}: {i.reason}" for i in item.ignored_fields)
                unread.add(Adw.ActionRow(title=item.file_name, subtitle=reason, subtitle_lines=3))
            page.add(unread)

        # Like the Mac's import sheet: everything to check in one list, the password notice last.
        if password_notice:
            notes.append(password_notice)
        if notes:
            notes_group = Adw.PreferencesGroup(title=_("Notes"))
            for note in notes:
                row = Adw.ActionRow(title=note, title_lines=0, use_markup=False, css_classes=["wb-import-note"])
                row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic", valign=Gtk.Align.CENTER,
                                         css_classes=["dim-label"]))
                notes_group.add(row)
            page.add(notes_group)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(page)
        self.set_child(toolbar)
        self._update_button()

    def _selected(self) -> set[str]:
        return {entry_id for entry_id, check in self._checks.items() if check.get_active() and check.get_sensitive()}

    def _update_button(self) -> None:
        count = len(self._selected())
        self.import_button.set_label(ngettext("Import {n} Connection", "Import {n} Connections", count).format(n=count))
        self.import_button.set_sensitive(count > 0)

    def _apply(self, *_args) -> None:
        selected = self._selected()
        try:
            self.app.library.apply_import(self.plan, selected)
        except Exception as error:  # noqa: BLE001
            log.exception("import failed")
            _alert(self, _("Import failed"), str(error))
            return
        self.close()
        window = self.app.main_window()
        if window is not None:
            window.show_toast(ngettext("Imported {n} connection.", "Imported {n} connections.",
                                       len(selected)).format(n=len(selected)))
