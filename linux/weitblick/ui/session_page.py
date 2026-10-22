"""One session tab (port of SessionWindowController and LiveSession).

The page keeps the view, the status and the agent state for its whole life; every connection
attempt is a LiveSession, so signing in again or "Reconnect" stays in the same tab.

Agent sessions (opened over MCP, `agent_desktop` set) use the same page, but it has no tab until the
user watches, the desktop has a fixed size, the clipboard is private and nothing that needs the user
(sign-in, certificate) is ever asked: it fails instead.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Callable

from gi.repository import Adw, Gdk, GLib, GObject, Gtk

from .. import bridge
from ..i18n import _
from ..keyboard import layouts
from ..keyboard.session_keyboard import SessionKeyboard, TextInput, is_app_shortcut
from ..rdp.session import (CertificateDecision, DisconnectReason, RDPSession, ServerCertificate, SessionConfig)
from . import activity as act
from .dialogs import Credentials, SignInDialog, confirm_disconnect, present_certificate_dialog, show_message
from .session_view import SessionView

log = logging.getLogger(__name__)

#: Waiting time after the last size change before asking the server to resize.
RESIZE_DEBOUNCE_MS = 300


def desktop_for_view(display, width: float, height: float, backing_scale: float) -> tuple[int, int, int]:
    """Remote desktop size and Windows scale (percent) for a view of `width` x `height` units
    (port of DisplaySettings.desktop(forViewSize:backingScale:)). HiDPI ("retina") asks for one
    remote pixel per device pixel."""
    density = backing_scale if display.retina else 1.0
    if display.follows_window:
        w, h = round(width * density), round(height * density)
    else:
        w, h = display.fixed_width, display.fixed_height
    w = min(max(w, 200), 8192) & ~1
    h = min(max(h, 200), 8192)
    percent = display.desktop_scale_factor or round(density * 100)
    return w, h, min(max(percent, 100), 500)


def lock_key_state() -> tuple[bool, bool, bool]:
    display = Gdk.Display.get_default()
    seat = display.get_default_seat() if display else None
    keyboard = seat.get_keyboard() if seat else None
    if keyboard is None:
        return (False, True, False)
    return (keyboard.get_caps_lock_state(), keyboard.get_num_lock_state(), keyboard.get_scroll_lock_state())


@dataclass
class AgentControl:
    """An agent controls the session; paused while the user has taken over."""

    client: str
    paused: bool = False


class LiveSession:
    """One connection attempt: the RDP session with its keyboard and clipboard."""

    def __init__(self, config: SessionConfig, text_input: TextInput, clipboard_enabled: bool, isolated_clipboard: bool):
        self.session = RDPSession(config)
        self.keyboard = SessionKeyboard(self.session, text_input, config.keyboard_layout)
        #: Typed in the sign-in dialog with "Save in keyring": stored once the server accepts them.
        self.credentials_to_save: Credentials | None = None
        self.clipboard = None
        try:
            from ..clipboard.sync import ClipboardSync
        except ImportError:  # pragma: no cover - clipboard module missing
            log.warning("clipboard sync unavailable", exc_info=True)
        else:
            if config.clipboard:
                display = Gdk.Display.get_default()
                local = None if isolated_clipboard or display is None else display.get_clipboard()
                self.clipboard = ClipboardSync(self.session.clipboard, local)
                self.clipboard.enabled = clipboard_enabled

    @property
    def is_connected(self) -> bool:
        return self.session.is_connected

    def end(self) -> None:
        """Lets go of the session: no more input, the clipboard keeps what it can, the bridge shuts down."""
        if self.clipboard is not None:
            self.clipboard.stop_watching()
            self.clipboard.session_will_end()
        self.session.close()


class SessionPage(Adw.Bin):
    __gtype_name__ = "WBSessionPage"
    __gsignals__ = {
        # The status (and tab indicator) changed.
        "status-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        # The page wants its tab closed (deliberate end, "Close Tab").
        "close-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
        # Keyboard focus and shortcut capture must be re-evaluated (connected, agent took over, …).
        "input-state-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        # The agent state changed (overview badge, agent host).
        "agent-control-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        # The page is gone for good.
        "closed": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, connection, library, settings, activity: act.SessionActivity,
                 agent_desktop: tuple[int, int] | None = None, session_id: str | None = None):
        super().__init__()
        self.connection = connection
        self.connection_id: str = connection.id
        self.library = library
        self.settings = settings
        self.activity = activity
        #: The id agents use for this session (list_sessions, tools).
        self.session_id = session_id or uuid.uuid4().hex[:8]
        #: Set for sessions an agent opened: their fixed desktop size.
        self.agent_desktop = agent_desktop
        self.status = act.CONNECTING
        self.live: LiveSession | None = None
        #: Session menu changes (text input, clipboard) last for this tab, across reconnects.
        self.text_input = TextInput.parse(connection.keyboard.text_input.value)
        self.clipboard_enabled = agent_desktop is None and connection.redirection.clipboard
        self._last_credentials: Credentials | None = None
        self._pending_resize = 0
        self._pending_certificate: Callable[[CertificateDecision], None] | None = None
        self._certificate_dialog = None
        self._sign_in_dialog = None
        self._closing = False
        self._has_focus = False
        self.agent_control: AgentControl | None = None
        #: Why an agent session could not connect or ended (certificate, missing password, …).
        self.agent_end_message: str | None = None
        self._connect_waiters: list[Callable[[str | None], None]] = []
        #: The page has been shown in a tab (agent sessions start without).
        self.was_presented = False

        self.view = SessionView()
        self.view.connect("geometry-changed", lambda *_: self._schedule_resolution_update())
        self._toasts = Adw.ToastOverlay()
        overlay = Gtk.Overlay()
        overlay.set_child(self.view)
        self._status_box = self._build_status_overlay()
        overlay.add_overlay(self._status_box)
        self._agent_banner = self._build_agent_banner()
        overlay.add_overlay(self._agent_banner)
        self._toasts.set_child(overlay)
        self.set_child(self._toasts)
        self._set_status(act.CONNECTING)

    # MARK: Properties

    @property
    def is_agent_session(self) -> bool:
        return self.agent_desktop is not None

    @property
    def is_connected(self) -> bool:
        return self.live is not None and self.live.is_connected

    @property
    def accepts_local_input(self) -> bool:
        """The user's input reaches the session (no agent, or the user took over)."""
        return self.agent_control is None or self.agent_control.paused

    @property
    def connected_session(self) -> RDPSession | None:
        return self.live.session if self.is_connected else None

    @property
    def clipboard(self):
        return self.live.clipboard if self.live else None

    @property
    def title(self) -> str:
        return self.connection.name

    @property
    def is_shown(self) -> bool:
        """The page sits in a tab of a window."""
        return self.get_root() is not None

    # MARK: Connecting

    def start(self) -> None:
        """Call once the page is in a window (agent sessions: right away); the view size decides the
        desktop size."""
        self._connect_with_saved_credentials()

    def reconnect(self) -> None:
        if self._last_credentials is not None:
            self._connect(self._last_credentials)
        else:
            self._connect_with_saved_credentials()

    def _connect_with_saved_credentials(self) -> None:
        password = self.library.password(self.connection_id)
        # With NLA the server checks the password before showing anything: ask first.
        if not self.connection.security.disable_nla and (password is None or not self.connection.username):
            if self.is_agent_session:
                self._agent_session_failed(self._missing_credentials_message())
            else:
                self.ask_for_credentials(None)
            return
        self._connect(Credentials(self.connection.username, self.connection.domain, password or ""))

    def _connect(self, credentials: Credentials, save_after_logon: bool = False) -> None:
        self._end_live_session()
        self.agent_end_message = None
        latest = self.library.connection(self.connection_id)
        if latest is not None:
            self.connection = latest
        self._set_status(act.CONNECTING)
        self._show_overlay("connecting")
        if self.agent_desktop is not None:
            width, height = self.agent_desktop
            desktop_scale = 100
        else:
            view_w, view_h = self.view.get_width(), self.view.get_height()
            if view_w < 50 or view_h < 50:
                view_w, view_h = 1280, 800
            width, height, desktop_scale = desktop_for_view(self.connection.display, view_w, view_h,
                                                            self.view.backing_scale())
        layout = self.connection.keyboard.layout_override or layouts.current_klid()
        redirection = self.connection.redirection
        audio = {"local": bridge.AudioMode.LOCAL, "remote": bridge.AudioMode.REMOTE}.get(
            redirection.audio_playback.value, bridge.AudioMode.OFF)
        security = self.connection.security
        advanced = self.connection.advanced
        from .. import paths
        config = SessionConfig(
            host=self.connection.host, port=self.connection.port, username=credentials.username,
            domain=credentials.domain, password=credentials.password, width=width, height=height,
            desktop_scale=desktop_scale, keyboard_layout=layout,
            ignore_certificate=security.ignore_certificate_errors,
            trusted_fingerprints=list(security.trusted_certificate_fingerprints), nla=not security.disable_nla,
            console_session=security.console_session, alternate_shell=advanced.alternate_shell or "",
            working_directory=advanced.working_dir or "", load_balance_info=advanced.load_balance_info or "",
            audio=audio if not self.is_agent_session else bridge.AudioMode.OFF,
            microphone=redirection.microphone and not self.is_agent_session,
            printers=redirection.printers and not self.is_agent_session,
            drives=redirection.shared_drives() if not self.is_agent_session else [],
            clipboard=redirection.clipboard or self.is_agent_session,
            state_directory=str(paths.freerdp_state_dir()))
        try:
            live = LiveSession(config, self.text_input, self.clipboard_enabled, self.is_agent_session)
        except Exception:  # noqa: BLE001
            log.exception("creating the session failed")
            message = _("The session couldn’t be created.")
            if self.is_agent_session:
                self._agent_session_failed(message)
            else:
                self._show_failure(message)
            return
        live.credentials_to_save = credentials if save_after_logon else None
        self._last_credentials = credentials
        self.live = live
        session = live.session
        session.certificate_handler = self._decide_certificate
        session.connect("connected", self._on_connected)
        session.connect("reconnecting", self._on_reconnecting)
        session.connect("reconnected", self._on_reconnected)
        session.connect("disconnected", self._on_disconnected)
        if live.clipboard is not None and hasattr(live.clipboard, "on_status"):
            live.clipboard.on_status = self.show_toast
        self.view.set_session(session)
        session.start()

    def ask_for_credentials(self, message: str | None) -> None:
        if self._sign_in_dialog is not None or self.get_root() is None:
            return
        self._set_status(act.DISCONNECTED)
        self._show_overlay("signed-out", message or _("No password is saved for this connection."))

        def connect(credentials: Credentials, save: bool) -> None:
            self._connect(credentials, save_after_logon=save)

        dialog = SignInDialog(self.connection.name, message, self.connection.username, self.connection.domain,
                              connect)
        dialog.connect("closed", lambda *_: setattr(self, "_sign_in_dialog", None))
        self._sign_in_dialog = dialog
        dialog.present(self)

    def _save_accepted_credentials(self, live: LiveSession) -> None:
        """The server accepted the credentials from the sign-in dialog: keep them if asked to."""
        credentials = live.credentials_to_save
        if credentials is None:
            return
        live.credentials_to_save = None
        try:
            self.library.set_password(credentials.password, self.connection_id)
        except Exception as error:  # noqa: BLE001
            log.error("saving the password failed: %s", error)
            show_message(self, _("Password not saved"),
                         _("The keyring didn’t accept the password. The session continues anyway."))
        if credentials.username != self.connection.username or credentials.domain != self.connection.domain:
            def change(stored) -> None:
                stored.username = credentials.username
                stored.domain = credentials.domain
            try:
                self.connection = self.library.modify(self.connection_id, change)
            except Exception:  # noqa: BLE001
                log.exception("updating the user name failed")

    # MARK: Session events

    def _is_current(self, session: RDPSession) -> bool:
        return self.live is not None and self.live.session is session

    def _on_connected(self, session: RDPSession) -> None:
        if not self._is_current(session):
            return
        self._session_did_connect()
        self._save_accepted_credentials(self.live)
        try:
            self.library.mark_connected(self.connection_id)
        except Exception:  # noqa: BLE001
            log.exception("marking the connection as used failed")

    def _on_reconnecting(self, session: RDPSession, attempt: int) -> None:
        if not self._is_current(session):
            return
        self._set_status(act.SessionStatus("reconnecting", attempt))
        self._show_overlay("reconnecting", attempt=attempt)
        self.live.keyboard.focus_lost()
        self.emit("input-state-changed")

    def _on_reconnected(self, session: RDPSession) -> None:
        if self._is_current(session):
            self._session_did_connect()

    def _session_did_connect(self) -> None:
        self._set_status(act.CONNECTED)
        self._show_overlay(None)
        if self._has_focus and self.accepts_local_input:
            self.live.keyboard.focus_gained(*lock_key_state())
            if self.live.clipboard is not None and not self.is_agent_session:
                self.live.clipboard.start_watching()
        if self.live.clipboard is not None and self.agent_control is not None and not self.agent_control.paused:
            self.live.clipboard.enabled = False
        if self.get_root() is not None:
            self.view.grab_focus()
        self.emit("input-state-changed")
        self._schedule_resolution_update()  # the window may have changed while connecting
        self._resume_connect_waiters(None)

    def _on_disconnected(self, session: RDPSession, reason: DisconnectReason, _code: int, _detail: str) -> None:
        if not self._is_current(session):
            return
        self._end_live_session()
        self.emit("input-state-changed")
        if self.is_agent_session:
            message = self.agent_end_message or (
                self._rejected_credentials_message() if reason.needs_credentials else reason.message)
            self._agent_session_failed(message, deliberate=reason.is_deliberate)
            return
        if self._closing:
            return
        self._answer_certificate(CertificateDecision.REJECT)
        if reason.is_deliberate:
            self.emit("close-requested")
        elif reason.needs_credentials:
            self.ask_for_credentials(reason.message)
        else:
            self._show_failure(reason.message)

    def _decide_certificate(self, certificate: ServerCertificate, reply: Callable[[CertificateDecision], None]) -> None:
        # An agent never decides about a certificate: the user connects once by hand.
        if self.is_agent_session:
            self.agent_end_message = self._certificate_message(certificate.changed)
            reply(CertificateDecision.REJECT)
            return
        if self._closing or self.get_root() is None or self._pending_certificate is not None:
            reply(CertificateDecision.REJECT)
            return
        connection_id = self.connection_id

        def decided(decision: CertificateDecision) -> None:
            if decision is CertificateDecision.ACCEPT_PERMANENTLY:
                try:
                    self.library.trust(certificate.fingerprint, connection_id, replacing=certificate.changed)
                except Exception:  # noqa: BLE001
                    log.exception("storing the certificate failed")
            self._answer_certificate(decision)

        self._pending_certificate = reply
        self._certificate_dialog = present_certificate_dialog(self, certificate, self.connection.name, decided)

    def _answer_certificate(self, decision: CertificateDecision) -> None:
        reply, self._pending_certificate = self._pending_certificate, None
        dialog, self._certificate_dialog = self._certificate_dialog, None
        if reply is not None:
            if dialog is not None and decision is CertificateDecision.REJECT:
                dialog.force_close()
            reply(decision)

    def _show_failure(self, message: str) -> None:
        self._set_status(act.DISCONNECTED)
        self._show_overlay("failed", message)

    def _end_live_session(self) -> None:
        """Lets go of the current attempt (if any); the tab and its last picture stay."""
        live, self.live = self.live, None
        if live is None:
            return
        self.view.set_session(None)
        live.end()

    # MARK: Focus and input (driven by the window)

    def focus_gained(self) -> None:
        self._has_focus = True
        if self.is_connected and self.accepts_local_input:
            self.live.keyboard.focus_gained(*lock_key_state())
            if self.live.clipboard is not None and not self.is_agent_session:
                self.live.clipboard.check_local()
                self.live.clipboard.start_watching()
        if self.get_root() is not None:
            self.view.grab_focus()

    def focus_lost(self) -> None:
        self._has_focus = False
        if self.live is not None:
            if self.live.is_connected and self.accepts_local_input:
                self.live.keyboard.focus_lost()
            if self.live.clipboard is not None:
                self.live.clipboard.stop_watching()

    def handle_key(self, keycode: int, keyval: int, state: Gdk.ModifierType, level: int, pressed: bool) -> bool:
        """A key event of the window while this tab is selected; True when the session took it."""
        if is_app_shortcut(state):
            if pressed and self.live is not None and self.live.is_connected:
                self.live.keyboard.app_shortcut_used()
            return False
        if not self.is_connected:
            return False
        if not self.accepts_local_input:
            return True  # while an agent types, the user's keys go nowhere
        return self.live.keyboard.handle(keycode, keyval, state, level, pressed)

    def wants_system_shortcuts(self, window_active: bool, fullscreen: bool) -> bool:
        """Whether Alt+Tab, Super and friends should go to Windows now (see settings)."""
        if not (self.is_connected and self.accepts_local_input and window_active):
            return False
        mode = self.settings.system_shortcut_capture
        return mode == "always" or (mode == "fullscreen" and fullscreen)

    # MARK: Session menu

    def send_chord(self, notation: str) -> None:
        if self.is_connected and self.accepts_local_input:
            self.live.keyboard.tap(notation)

    def set_text_input(self, text_input: TextInput) -> None:
        self.text_input = text_input
        if self.live is not None:
            self.live.keyboard.text_input = text_input

    def set_clipboard_enabled(self, enabled: bool) -> None:
        self.clipboard_enabled = enabled
        if self.live is not None and self.live.clipboard is not None:
            self.live.clipboard.enabled = enabled

    @property
    def can_reconnect(self) -> bool:
        return self.status == act.DISCONNECTED and not self.is_agent_session and self._sign_in_dialog is None

    def show_toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=message, timeout=4))

    # MARK: Resolution

    def _schedule_resolution_update(self) -> None:
        if self._pending_resize:
            GLib.source_remove(self._pending_resize)
            self._pending_resize = 0
        if self.is_agent_session or not self.connection.display.resizes_with_window:
            return
        self._pending_resize = GLib.timeout_add(RESIZE_DEBOUNCE_MS, self._send_resolution)

    def _send_resolution(self) -> bool:
        self._pending_resize = 0
        if not self.is_connected or self.get_root() is None:
            return GLib.SOURCE_REMOVE
        width, height = self.view.get_width(), self.view.get_height()
        if width < 50 or height < 50:
            return GLib.SOURCE_REMOVE
        w, h, scale = desktop_for_view(self.connection.display, width, height, self.view.backing_scale())
        log.info("requesting remote size %dx%d at %d%%", w, h, scale)
        self.live.session.set_resolution(w, h, scale, 100)
        return GLib.SOURCE_REMOVE

    # MARK: Agent sessions

    def wait_until_connected(self, timeout: float, callback: Callable[[str | None], None]) -> None:
        """callback(None) once connected, or callback(reason) if it failed; at most `timeout` seconds."""
        if self.is_connected:
            callback(None)
            return
        if self.agent_end_message and self.live is None:
            callback(self.agent_end_message)
            return
        done = [False]

        def finish(message: str | None) -> None:
            if not done[0]:
                done[0] = True
                callback(message)

        def expire() -> bool:
            if finish in self._connect_waiters:
                self._connect_waiters.remove(finish)
                finish(f"The connection to “{self.connection.name}” took longer than {int(timeout)} seconds.")
            return GLib.SOURCE_REMOVE

        self._connect_waiters.append(finish)
        GLib.timeout_add(int(timeout * 1000), expire)

    def _resume_connect_waiters(self, message: str | None) -> None:
        waiters, self._connect_waiters = self._connect_waiters, []
        for waiter in waiters:
            waiter(message)

    def _agent_session_failed(self, message: str, deliberate: bool = False) -> None:
        """An agent session could not connect or ended: tell the waiting tool, then let the page go
        unless the user watches it."""
        self.agent_end_message = message
        self._end_live_session()
        self._resume_connect_waiters(message)
        if self._closing:
            return
        if deliberate or not self.is_shown:
            self.emit("close-requested")
            if not self.is_shown:
                self.close()
        else:
            self._show_failure(message)

    def attach_agent(self, client: str) -> None:
        """An agent takes control (user session: attach). Local input is blocked until the user takes over."""
        self.agent_control = AgentControl(client)
        self._agent_control_changed()

    def detach_agent(self) -> None:
        if self.agent_control is not None:
            self.agent_control = None
            self._agent_control_changed()

    def take_over_from_agent(self) -> None:
        if self.agent_control is not None and not self.agent_control.paused:
            self.agent_control.paused = True
            self._agent_control_changed()

    def resume_agent(self) -> None:
        if self.agent_control is not None and self.agent_control.paused:
            self.agent_control.paused = False
            self._agent_control_changed()

    def _agent_control_changed(self) -> None:
        self.view.accepts_local_input = self.accepts_local_input
        if self.live is not None:
            if not self.accepts_local_input:
                # The user's keys must not stay down while the agent types.
                self.live.keyboard.focus_lost()
                if self.live.clipboard is not None:
                    self.live.clipboard.enabled = False
            else:
                if self.live.clipboard is not None:
                    self.live.clipboard.enabled = self.clipboard_enabled
                if self._has_focus and self.live.is_connected:
                    self.live.keyboard.focus_gained(*lock_key_state())
        self._update_agent_banner()
        self.publish_agent_activity()
        self.emit("input-state-changed")
        self.emit("agent-control-changed")

    def publish_agent_activity(self) -> None:
        if self._closing:
            return
        control = self.agent_control
        agent = None if control is None else act.AgentActivity(control.client, control.paused, not self.is_shown)
        self.activity.set_agent(self.connection_id, agent)

    # Messages for agents: English whatever the UI language (the model reads them).

    def _missing_credentials_message(self) -> str:
        return (f"No user name or password is saved for “{self.connection.name}”. Ask the user to connect once by "
                "hand in Weitblick Remote and save the password.")

    def _rejected_credentials_message(self) -> str:
        return (f"Windows rejected the saved user name or password for “{self.connection.name}”. Ask the user to "
                "connect once by hand in Weitblick Remote and save the right password.")

    def _certificate_message(self, changed: bool) -> str:
        if changed:
            return (f"The certificate of “{self.connection.name}” has changed. Ask the user to connect once by hand "
                    "in Weitblick Remote and check it.")
        return (f"The certificate of “{self.connection.name}” isn’t trusted yet. Ask the user to connect once by "
                "hand in Weitblick Remote and trust it.")

    # MARK: Status and overlays

    def _set_status(self, status: act.SessionStatus) -> None:
        self.status = status
        self.activity.set_status(self.connection_id, status)
        self.emit("status-changed")

    def _build_status_overlay(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER,
                      valign=Gtk.Align.CENTER, css_classes=["wb-session-status", "osd"])
        self._status_spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        self._status_icon = Gtk.Image(pixel_size=48)
        self._status_title = Gtk.Label(css_classes=["title-3"], wrap=True, justify=Gtk.Justification.CENTER)
        self._status_message = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=48)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=6)
        self._retry_button = Gtk.Button(label=_("Reconnect"), css_classes=["pill", "suggested-action"])
        self._retry_button.connect("clicked", lambda *_: self.reconnect())
        self._sign_in_button = Gtk.Button(label=_("Sign In…"), css_classes=["pill", "suggested-action"])
        self._sign_in_button.connect("clicked", lambda *_: self.ask_for_credentials(None))
        self._close_button = Gtk.Button(label=_("Close Tab"), css_classes=["pill"])
        self._close_button.connect("clicked", lambda *_: self.emit("close-requested"))
        for widget in (self._retry_button, self._sign_in_button, self._close_button):
            buttons.append(widget)
        for widget in (self._status_spinner, self._status_icon, self._status_title, self._status_message, buttons):
            box.append(widget)
        return box

    def _show_overlay(self, phase: str | None, message: str = "", attempt: int = 0) -> None:
        """phase: None (hidden), "connecting", "reconnecting", "failed", "signed-out"."""
        box = self._status_box
        box.set_visible(phase is not None)
        if phase is None:
            return
        busy = phase in ("connecting", "reconnecting")
        self._status_spinner.set_visible(busy)
        self._status_icon.set_visible(not busy)
        self._status_icon.set_from_icon_name("dialog-password-symbolic" if phase == "signed-out"
                                             else "network-offline-symbolic")
        if phase == "connecting":
            self._status_title.set_label(_("Connecting to {host}…").format(host=self.connection.host))
            self._status_message.set_label("")
        elif phase == "reconnecting":
            self._status_title.set_label(_("Connection lost"))
            self._status_message.set_label(_("Reconnecting, attempt {n}…").format(n=attempt))
        elif phase == "failed":
            self._status_title.set_label(self.connection.name)
            self._status_message.set_label(message)
        else:
            self._status_title.set_label(_("Sign in to “{name}”").format(name=self.connection.name))
            self._status_message.set_label(message)
        self._status_message.set_visible(bool(self._status_message.get_label()))
        self._retry_button.set_visible(phase == "failed" and not self.is_agent_session)
        self._sign_in_button.set_visible(phase == "signed-out")
        self._close_button.set_visible(phase in ("failed", "signed-out"))

    def _build_agent_banner(self) -> Gtk.Widget:
        banner = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, valign=Gtk.Align.START, margin_top=8,
                         css_classes=["wb-agent-banner", "osd", "toolbar"], visible=False)
        self._agent_label = Gtk.Label()
        self._agent_button = Gtk.Button(css_classes=["pill"])
        self._agent_button.connect("clicked", self._on_agent_button)
        banner.append(Gtk.Image(icon_name="weitblick-agent-symbolic"))
        banner.append(self._agent_label)
        banner.append(self._agent_button)
        return banner

    def _update_agent_banner(self) -> None:
        control = self.agent_control
        self._agent_banner.set_visible(control is not None)
        if control is None:
            return
        if control.paused:
            self._agent_label.set_label(_("You have taken over from the agent ({client})").format(client=control.client))
            self._agent_button.set_label(_("Resume agent"))
        else:
            self._agent_label.set_label(_("An agent is controlling this session ({client})").format(client=control.client))
            self._agent_button.set_label(_("Take over"))

    def _on_agent_button(self, *_args) -> None:
        if self.agent_control is None:
            return
        if self.agent_control.paused:
            self.resume_agent()
        else:
            self.take_over_from_agent()

    # MARK: Closing

    def request_close(self, answer: Callable[[bool], None]) -> None:
        """The user closes the tab: asks first (if set) while a session runs."""
        if self.live is None or not self.settings.confirm_close_session or self.get_root() is None:
            answer(True)
            return

        def answered(disconnect: bool, dont_ask: bool) -> None:
            if disconnect and dont_ask:
                self.settings.confirm_close_session = False
            answer(disconnect)

        confirm_disconnect(self, self.connection.name, self.is_agent_session, answered)

    def close(self) -> None:
        """Ends everything; the page is not used afterwards."""
        if self._closing:
            return
        self._closing = True
        if self._pending_resize:
            GLib.source_remove(self._pending_resize)
            self._pending_resize = 0
        self._answer_certificate(CertificateDecision.REJECT)
        if self.agent_end_message is None and self.is_agent_session:
            self.agent_end_message = f"The session “{self.connection.name}” was closed."
        self._resume_connect_waiters(self.agent_end_message or f"The session “{self.connection.name}” was closed.")
        self._end_live_session()
        self.activity.set_status(self.connection_id, None)
        self.emit("closed")
