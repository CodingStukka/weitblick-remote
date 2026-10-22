"""The agent parts of the interface: the permission prompt (port of AgentPromptPanel.swift) and the
"AI agents (MCP)" group of the settings dialog."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from .. import paths  # noqa: E402
from ..i18n import _  # noqa: E402
from .access_gate import AccessGrant, AccessPrompt, PromptAnswer  # noqa: E402


def setup_command(which: Callable[[str], str | None] = shutil.which, home: Path | None = None,
                  executable: str | None = None, source_root: Path | None = None) -> str:
    """The `claude mcp add` line for this copy of the app: the installed weitblick-mcp launcher, or the
    helper module of this source checkout (with PYTHONPATH, since Claude Code starts it anywhere)."""
    launcher = which("weitblick-mcp")
    if launcher is None:
        local = (home or Path.home()) / ".local" / "bin" / "weitblick-mcp"
        if local.exists():
            launcher = str(local)
    if launcher is not None:
        return f"claude mcp add weitblick -- {shlex.quote(launcher)}"
    root = str(source_root or paths.SOURCE_ROOT)
    python = executable or sys.executable or "python3"
    return (f"claude mcp add weitblick -e PYTHONPATH={shlex.quote(root)} -- {shlex.quote(python)} "
            "-m weitblick.mcp_helper")


def grant_scope(grant: AccessGrant) -> str:
    if grant.session_id is not None:
        return _("This session ({id})").format(id=grant.session_id)
    if grant.until is not None:
        return _("Until {time}").format(time=time.strftime("%H:%M", time.localtime(grant.until)))
    return ""


def _short_path(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


# MARK: - Permission prompt

class PromptWindow(Adw.Window):
    """A small window that asks the user about one agent request. It doesn't take the keyboard
    focus when it appears (the user may be typing in a session), and shows even while the app runs
    without a main window."""

    __gtype_name__ = "WBAgentPromptWindow"

    def __init__(self, app, prompt: AccessPrompt, answer: Callable[[PromptAnswer], None]):
        super().__init__(application=app, title=_("AI agent access"), resizable=False, default_width=460,
                         deletable=False)
        self._answer = answer
        self._answered = False
        request = prompt.request

        icon = Gtk.Image(icon_name="weitblick-agent-symbolic", pixel_size=32, valign=Gtk.Align.START,
                         css_classes=["accent"])
        heading = Gtk.Label(label=_("“{client}” wants to control “{connection}”").format(
            client=request.client, connection=request.connection_name), wrap=True, xalign=0,
            css_classes=["heading"], max_width_chars=40)
        body = Gtk.Label(label=_("{host}: screenshots, mouse, keyboard and clipboard of this Windows session. The "
                                 "agent waits up to 2 minutes for your answer.").format(host=request.host),
                         wrap=True, xalign=0, css_classes=["dim-label"], max_width_chars=40)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        text.append(heading)
        text.append(body)
        top = Gtk.Box(spacing=12)
        top.append(icon)
        top.append(text)

        def button(label: str, answer_value: PromptAnswer, tooltip: str | None = None,
                   css: list[str] | None = None) -> Gtk.Button:
            widget = Gtk.Button(label=label, tooltip_text=tooltip, css_classes=css or [], focus_on_click=False)
            widget.connect("clicked", lambda *_: self._reply(answer_value))
            return widget

        first = Gtk.Box(spacing=8)
        first.append(button(_("Deny"), PromptAnswer.DENY))
        first.append(Gtk.Box(hexpand=True))
        first.append(button(_("Allow once"), PromptAnswer.ALLOW_ONCE, _("This session, until it disconnects")))
        second = Gtk.Box(spacing=8)
        second.append(button(_("Allow for 10 minutes"), PromptAnswer.ALLOW_TEN_MINUTES))
        second.append(button(_("Allow today"), PromptAnswer.ALLOW_TODAY, _("Until midnight")))
        second.append(Gtk.Box(hexpand=True))
        second.append(button(_("Always allow"), PromptAnswer.ALWAYS_ALLOW,
                             _("Sets this connection’s agent access to Always allow"), ["suggested-action"]))

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=12, margin_bottom=16,
                          margin_start=16, margin_end=16)
        content.append(top)
        content.append(first)
        content.append(second)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False))
        toolbar.set_content(content)
        self.set_content(toolbar)
        self.set_focus_visible(False)
        self.connect("realize", self._on_realize)
        self.connect("close-request", self._on_close_request)

    def _on_realize(self, *_args) -> None:
        # X11: a user time of 0 tells the window manager not to focus the window when it maps.
        surface = self.get_surface()
        try:
            import gi
            gi.require_version("GdkX11", "4.0")
            from gi.repository import GdkX11
            if isinstance(surface, GdkX11.X11Surface):
                surface.set_user_time(0)
                surface.set_skip_taskbar_hint(True)
        except (ImportError, ValueError, AttributeError):
            pass

    def _reply(self, answer: PromptAnswer) -> None:
        if self._answered:
            return
        self._answered = True
        self._answer(answer)

    def _on_close_request(self, *_args) -> bool:
        # Closing the prompt counts as Deny (this request only).
        self._reply(PromptAnswer.DENY)
        return False


class PromptWindows:
    """One prompt window per open prompt, driven by AccessGate.show_prompt / close_prompt."""

    def __init__(self, app, on_answer: Callable[[int, PromptAnswer], None]):
        self.app = app
        self.on_answer = on_answer
        self._windows: dict[int, PromptWindow] = {}

    def show(self, prompt: AccessPrompt) -> None:
        if prompt.id in self._windows:
            return
        window = PromptWindow(self.app, prompt, lambda answer: self.on_answer(prompt.id, answer))
        self._windows[prompt.id] = window
        # set_visible maps without asking for activation (present() would raise and focus it).
        window.set_visible(True)
        display = Gdk.Display.get_default()
        if display is not None:
            display.beep()

    def close(self, prompt: AccessPrompt) -> None:
        window = self._windows.pop(prompt.id, None)
        if window is not None:
            window.destroy()


# MARK: - Settings

class AgentSettingsGroup(Adw.PreferencesGroup):
    """Settings → "AI agents (MCP)": the master switch, the setup line, the time grants and the log."""

    __gtype_name__ = "WBAgentSettingsGroup"

    def __init__(self, app):
        super().__init__(
            title=_("AI agents (MCP)"),
            description=_("Agents connect through the weitblick-mcp helper. Each connection decides in its Security "
                          "settings whether agents are asked about, always allowed or never allowed. Everything "
                          "agents do is logged in {path}.").format(path=_short_path(paths.agent_log_file())))
        self.app = app
        self._host = None
        self._listener = 0
        self._grant_rows: list[Gtk.Widget] = []
        self._timer = 0

        self.switch = Adw.SwitchRow(title=_("Let AI agents control Windows sessions"),
                                    active=app.settings.agent_control_enabled)
        self.switch.connect("notify::active", self._switch_toggled)
        self.add(self.switch)

        self.error_row = Adw.ActionRow(css_classes=["error"], visible=False)
        self.error_row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
        self.add(self.error_row)

        command = setup_command()
        self.setup_row = Adw.ActionRow(title=_("Set up Claude Code"), subtitle=command, subtitle_selectable=True)
        self.setup_row.add_css_class("property")
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text=_("Copy"), valign=Gtk.Align.CENTER,
                          css_classes=["flat"])
        copy.connect("clicked", lambda *_: self._copy(command))
        self.setup_row.add_suffix(copy)
        self.add(self.setup_row)

        self.grants_row = Adw.ActionRow(title=_("Permissions"), subtitle=_("None"))
        self.add(self.grants_row)

        self.log_row = Adw.ActionRow(title=_("Audit log"), subtitle=_short_path(paths.agent_log_file()),
                                     activatable=True)
        self.log_row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        self.log_row.connect("activated", lambda *_: self._open_log())
        self.add(self.log_row)

        self._settings_listener = app.settings.add_listener(self._setting_changed)
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        self._refresh()

    # The settings object calls listeners with the changed key.
    def _setting_changed(self, key: str) -> None:
        if key == "agentControlEnabled":
            GLib.idle_add(lambda: (self._refresh(), GLib.SOURCE_REMOVE)[1])

    def _switch_toggled(self, *_args) -> None:
        active = self.switch.get_active()
        if active != self.app.settings.agent_control_enabled:
            self.app.settings.agent_control_enabled = active
        self._refresh()

    def _on_map(self, *_args) -> None:
        # Grants expire silently: redraw every few seconds while the group is on screen.
        if not self._timer:
            self._timer = GLib.timeout_add_seconds(5, self._tick)
        self._refresh()

    def _on_unmap(self, *_args) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._attach_host(None)

    def do_unroot(self) -> None:
        self.app.settings.remove_listener(self._settings_listener)
        self._attach_host(None)
        Adw.PreferencesGroup.do_unroot(self)

    def _tick(self) -> bool:
        self._refresh_grants()
        return GLib.SOURCE_CONTINUE

    def _attach_host(self, host) -> None:
        if host is self._host:
            return
        if self._host is not None and self._listener:
            self._host.remove_grants_listener(self._listener)
        self._host, self._listener = host, 0
        if host is not None:
            self._listener = host.add_grants_listener(
                lambda: GLib.idle_add(lambda: (self._refresh(), GLib.SOURCE_REMOVE)[1]))

    def _refresh(self) -> None:
        enabled = self.app.settings.agent_control_enabled
        if self.switch.get_active() != enabled:
            self.switch.set_active(enabled)
        host = self.app.agent_host if enabled else None
        self._attach_host(host if self.get_mapped() else None)
        error = host.start_error if host is not None else None
        self.error_row.set_visible(error is not None)
        if error is not None:
            self.error_row.set_title(_("The MCP socket couldn’t be started: {error}").format(error=error))
        for row in (self.setup_row, self.grants_row, self.log_row):
            row.set_visible(enabled)
        self._refresh_grants()

    def _refresh_grants(self) -> None:
        for row in self._grant_rows:
            self.remove(row)
        self._grant_rows = []
        host = self.app.agent_host if self.app.settings.agent_control_enabled else None
        grants = host.current_grants() if host is not None else []
        self.grants_row.set_visible(host is not None and not grants)
        # The grants go between the permissions row and the log row.
        self.remove(self.log_row)
        for grant in grants:
            row = Adw.ActionRow(title=_("“{client}” on “{connection}”").format(
                client=grant.client, connection=grant.connection_name), subtitle=grant_scope(grant))
            revoke = Gtk.Button(label=_("Revoke"), valign=Gtk.Align.CENTER)
            revoke.connect("clicked", lambda _b, g=grant: self._revoke(g))
            row.add_suffix(revoke)
            self.add(row)
            self._grant_rows.append(row)
        self.add(self.log_row)

    def _revoke(self, grant: AccessGrant) -> None:
        if self.app.agent_host is not None:
            self.app.agent_host.revoke(grant)
        self._refresh_grants()

    def _copy(self, text: str) -> None:
        # The user's own click: the desktop clipboard is what they asked for.
        clipboard = self.get_clipboard()
        clipboard.set_content(Gdk.ContentProvider.new_for_value(GObject.Value(str, text)))
        dialog = self.get_ancestor(Adw.PreferencesDialog)
        if dialog is not None:
            dialog.add_toast(Adw.Toast(title=_("Copied")))

    def _open_log(self) -> None:
        path = paths.agent_log_file()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(descriptor)
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(path)).get_uri(), None)
        except GLib.Error:
            pass
