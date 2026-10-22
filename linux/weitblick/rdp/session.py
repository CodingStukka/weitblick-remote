"""One RDP connection: a GObject over the C bridge whose signals arrive on the GTK main loop.

    session = RDPSession(SessionConfig(host=..., username=..., password=..., width=1280, height=800))
    session.connect("connected", ...)          # GObject signals, see RDPSession.__gsignals__
    session.certificate_handler = ask_user      # (ServerCertificate, reply) -> None, reply once
    session.start()
    ...
    session.close()                             # tears the bridge down in a background thread

Port of WeitblickKit's RDPSession, EventRelay, RemoteClipboard and CertificateGate.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

from gi.repository import GLib, GObject

from .. import bridge, paths
from ..bridge import AudioMode, MouseButton, RawDisconnectReason
from ..i18n import _

log = logging.getLogger(__name__)

#: How long a connection waits for the certificate decision before rejecting.
CERTIFICATE_DECISION_TIMEOUT = 120.0


@dataclass
class SessionConfig:
    """Everything needed to open one RDP session (port of SessionConfiguration)."""

    host: str
    username: str = ""
    password: str = ""
    width: int = 1280
    height: int = 800
    port: int = 3389
    domain: str = ""
    #: Windows display scaling in percent (100…500).
    desktop_scale: int = 100
    #: 100, 140 or 180.
    device_scale: int = 100
    #: Windows keyboard layout id (KLID), e.g. 0x0407 German.
    keyboard_layout: int = 0x0409
    ignore_certificate: bool = False
    #: SHA-256 fingerprints (any separator, any case) accepted without asking.
    trusted_fingerprints: list[str] = field(default_factory=list)
    nla: bool = True
    console_session: bool = False
    alternate_shell: str = ""
    working_directory: str = ""
    load_balance_info: str = ""
    audio: AudioMode = AudioMode.LOCAL
    microphone: bool = False
    printers: bool = False
    #: (share name, local folder) pairs shown as \\tsclient\<name>.
    drives: list[tuple[str, str]] = field(default_factory=list)
    clipboard: bool = True
    auto_reconnect: bool = True
    state_directory: str = field(default_factory=lambda: str(paths.freerdp_state_dir()))


class DisconnectReason(Enum):
    """Why a session ended, with a message for the user."""

    REQUESTED = RawDisconnectReason.REQUESTED
    HOST_UNREACHABLE = RawDisconnectReason.HOST_UNREACHABLE
    LOGON_FAILED = RawDisconnectReason.LOGON_FAILED
    MISSING_CREDENTIALS = RawDisconnectReason.MISSING_CREDENTIALS
    ACCOUNT_LOCKED = RawDisconnectReason.ACCOUNT_LOCKED
    ACCOUNT_RESTRICTED = RawDisconnectReason.ACCOUNT_RESTRICTED
    PASSWORD_EXPIRED = RawDisconnectReason.PASSWORD_EXPIRED
    ACCESS_DENIED = RawDisconnectReason.ACCESS_DENIED
    CERTIFICATE_REJECTED = RawDisconnectReason.CERTIFICATE_REJECTED
    SECURITY_FAILED = RawDisconnectReason.SECURITY_FAILED
    NLA_REQUIRED = RawDisconnectReason.NLA_REQUIRED
    SERVER_ENDED = RawDisconnectReason.SERVER_ENDED
    TAKEN_OVER = RawDisconnectReason.TAKEN_OVER
    LOGGED_OFF = RawDisconnectReason.LOGGED_OFF
    TIMEOUT = RawDisconnectReason.TIMEOUT
    CONNECTION_LOST = RawDisconnectReason.CONNECTION_LOST
    OTHER = RawDisconnectReason.OTHER

    @classmethod
    def from_raw(cls, raw: int) -> DisconnectReason:
        try:
            return cls(RawDisconnectReason(raw))
        except ValueError:
            return cls.OTHER

    @property
    def needs_credentials(self) -> bool:
        """Asking for the password again can fix it."""
        return self in (DisconnectReason.LOGON_FAILED, DisconnectReason.MISSING_CREDENTIALS)

    @property
    def is_deliberate(self) -> bool:
        """Ended on purpose, by the user here or in Windows: nothing to report."""
        return self in (DisconnectReason.REQUESTED, DisconnectReason.LOGGED_OFF)

    @property
    def message(self) -> str:
        return {
            DisconnectReason.REQUESTED: _("Disconnected."),
            DisconnectReason.HOST_UNREACHABLE: _("Host unreachable."),
            DisconnectReason.LOGON_FAILED: _("Sign-in failed: wrong user name or password."),
            DisconnectReason.MISSING_CREDENTIALS: _("Sign-in failed: credentials missing."),
            DisconnectReason.ACCOUNT_LOCKED: _("Account locked."),
            DisconnectReason.ACCOUNT_RESTRICTED: _("Account disabled, expired or not allowed to sign in here."),
            DisconnectReason.PASSWORD_EXPIRED: _("Password expired. Set a new password in Windows."),
            DisconnectReason.ACCESS_DENIED: _(
                "Access denied: the user isn’t allowed to sign in via Remote Desktop."),
            DisconnectReason.CERTIFICATE_REJECTED: _("Certificate rejected."),
            DisconnectReason.SECURITY_FAILED: _("Secure connection failed (TLS/security negotiation)."),
            DisconnectReason.NLA_REQUIRED: _(
                "The server requires Network Level Authentication (NLA). Turn NLA on for this connection."),
            DisconnectReason.SERVER_ENDED: _("The server disconnected."),
            DisconnectReason.TAKEN_OVER: _("Another connection took over the session."),
            DisconnectReason.LOGGED_OFF: _("Session ended in Windows."),
            DisconnectReason.TIMEOUT: _("Timed out."),
            DisconnectReason.CONNECTION_LOST: _("Connection lost."),
            DisconnectReason.OTHER: _("Connection ended."),
        }[self]


@dataclass(frozen=True)
class ServerCertificate:
    """A server certificate without a valid chain (self-signed, the usual case for RDP)."""

    host: str
    port: int
    common_name: str
    subject: str
    issuer: str
    #: SHA-256, colon-separated uppercase hex.
    fingerprint: str
    hostname_mismatch: bool
    #: The connection trusts other fingerprints, so the certificate changed since it was trusted.
    changed: bool


class CertificateDecision(Enum):
    ACCEPT_ONCE = "once"
    #: Accept, and the app remembers the fingerprint for this connection.
    ACCEPT_PERMANENTLY = "permanently"
    REJECT = "reject"

    @property
    def accepts(self) -> bool:
        return self is not CertificateDecision.REJECT


def normalize_fingerprint(fingerprint: str) -> str:
    """Hex digits only, uppercase: `AA:BB…`, `aabb…` and `AA BB …` are the same fingerprint."""
    return "".join(ch for ch in fingerprint.upper() if ch in "0123456789ABCDEF")


class CertificateGate:
    """Decides about certificates on the FreeRDP thread. Trusted fingerprints and those accepted
    earlier in this session (reconnects) pass at once; anything else waits for the app's answer on
    the main loop, bounded by `timeout`. Thread safe."""

    def __init__(self, trusted: list[str], timeout: float = CERTIFICATE_DECISION_TIMEOUT):
        self._trusted = {normalize_fingerprint(f) for f in trusted}
        self._timeout = timeout
        self._lock = threading.Lock()
        self._accepted: set[str] = set()
        self._pending: dict[int, list] = {}  # id -> [event, decision]
        self._closed = False
        self._next_id = 0

    @property
    def trusts_other_certificates(self) -> bool:
        return bool(self._trusted)

    def is_trusted(self, fingerprint: str) -> bool:
        key = normalize_fingerprint(fingerprint)
        with self._lock:
            return key in self._trusted or key in self._accepted

    def decide(self, certificate: ServerCertificate,
               ask: Callable[[ServerCertificate, Callable[[CertificateDecision], None]], None]) -> bool:
        """Blocks the calling (FreeRDP) thread until `ask` answers, the timeout passes or `close` runs."""
        if self.is_trusted(certificate.fingerprint):
            return True
        event = threading.Event()
        with self._lock:
            if self._closed:
                return False
            self._next_id += 1
            pending_id = self._next_id
            entry = [event, CertificateDecision.REJECT]
            self._pending[pending_id] = entry

        def resolve(decision: CertificateDecision) -> None:
            with self._lock:
                found = self._pending.pop(pending_id, None)
            if found is not None:
                found[1] = decision
                found[0].set()

        ask(certificate, resolve)
        answered = event.wait(self._timeout)
        with self._lock:
            self._pending.pop(pending_id, None)
        if not answered:
            log.error("No answer about the certificate of %s within %d s, rejecting", certificate.host,
                      self._timeout)
            return False
        decision = entry[1]
        if decision.accepts:
            with self._lock:
                self._accepted.add(normalize_fingerprint(certificate.fingerprint))
        return decision.accepts

    def close(self) -> None:
        """Rejects every open question and all later ones; the session is going away."""
        with self._lock:
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for event, _decision in pending:
            event.set()


# MARK: - Pointer events

@dataclass(frozen=True)
class PointerImage:
    """A server cursor shape: BGRA32, straight alpha, top-down, width * 4 bytes per row."""

    id: int
    width: int
    height: int
    hotspot_x: int
    hotspot_y: int
    bgra: bytes


@dataclass(frozen=True)
class PointerEvent:
    """kind: "new" (image), "free" (id), "set" (id), "hidden", "default", "moved" (x, y)."""

    kind: str
    id: int = 0
    image: PointerImage | None = None
    x: int = 0
    y: int = 0


@dataclass
class Frame:
    """A copy of the framebuffer: BGRA32, top-down, `stride` bytes per row."""

    data: bytes
    width: int
    height: int
    stride: int
    #: (x, y, width, height) regions changed since the previous acquire (empty for a peek).
    dirty: list[tuple[int, int, int, int]] = field(default_factory=list)


# MARK: - Clipboard channel

@dataclass(frozen=True)
class RemoteClipboardFormat:
    id: int
    #: None for standard Windows formats (CF_UNICODETEXT, CF_DIB, …).
    name: str | None = None


@dataclass(frozen=True)
class RemoteFileRequest:
    """What the server asks of a file we announced in a FileGroupDescriptorW."""

    stream_id: int
    file_index: int
    #: True: answer with the size (8 bytes little endian), else with `length` bytes at `offset`.
    size_only: bool
    offset: int
    length: int


class RemoteClipboardDelegate(Protocol):
    """Clipboard channel events, delivered on the GTK main loop in order."""

    def remote_clipboard_ready(self, clipboard: RemoteClipboard) -> None: ...
    def remote_clipboard_announced(self, clipboard: RemoteClipboard, accepted: bool) -> None: ...
    def remote_clipboard_formats(self, clipboard: RemoteClipboard, formats: list[RemoteClipboardFormat]) -> None: ...
    def remote_clipboard_data_requested(self, clipboard: RemoteClipboard, format_id: int) -> None: ...
    def remote_clipboard_data_received(self, clipboard: RemoteClipboard, data: bytes | None) -> None: ...
    def remote_clipboard_file_requested(self, clipboard: RemoteClipboard, request: RemoteFileRequest) -> None: ...
    def remote_clipboard_file_received(self, clipboard: RemoteClipboard, stream_id: int, data: bytes | None) -> None: ...
    def remote_clipboard_closed(self, clipboard: RemoteClipboard) -> None: ...


class RemoteClipboard:
    """The clipboard channel of one session. Calls may come from any thread; they return False while
    the channel is down and after the session was closed. Requests and responses pair up in order
    (the protocol has no ids): keep at most one data request outstanding."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: int | None = None
        self.delegate: RemoteClipboardDelegate | None = None

    def _call(self, body: Callable[[int], bool]) -> bool:
        with self._lock:  # held during the call so detach() waits for calls in progress
            if self._session is None:
                return False
            return bool(body(self._session))

    def announce(self, formats: list[RemoteClipboardFormat]) -> bool:
        array = (bridge.WBClipboardFormat * max(len(formats), 1))()
        for index, fmt in enumerate(formats):
            array[index].id = fmt.id
            array[index].name = bridge.encode(fmt.name)
        return self._call(lambda s: bridge.lib().wb_session_clipboard_announce(s, array, len(formats)))

    def request(self, format_id: int) -> bool:
        return self._call(lambda s: bridge.lib().wb_session_clipboard_request(s, format_id))

    def respond(self, data: bytes | None) -> bool:
        """Answers a data request; None reports failure."""
        if data is None:
            return self._call(lambda s: bridge.lib().wb_session_clipboard_respond(s, False, None, 0))
        return self._call(lambda s: bridge.lib().wb_session_clipboard_respond(s, True, data, len(data)))

    def request_file(self, request: RemoteFileRequest) -> bool:
        return self._call(lambda s: bridge.lib().wb_session_clipboard_file_request(
            s, request.stream_id, request.file_index, request.size_only, request.offset, request.length))

    def respond_file(self, stream_id: int, data: bytes | None) -> bool:
        if data is None:
            return self._call(lambda s: bridge.lib().wb_session_clipboard_file_respond(s, stream_id, False, None, 0))
        return self._call(
            lambda s: bridge.lib().wb_session_clipboard_file_respond(s, stream_id, True, data, len(data)))

    # Session side

    def _attach(self, session: int) -> None:
        with self._lock:
            self._session = session

    def _detach(self) -> None:
        with self._lock:
            self._session = None

    def _notify(self, method: str, *args) -> None:
        delegate = self.delegate
        if delegate is not None:
            getattr(delegate, method)(self, *args)


# MARK: - Session

class SessionState(Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"


_teardowns: set[threading.Thread] = set()
_teardowns_lock = threading.Lock()


def wait_for_all_sessions(timeout: float) -> bool:
    """Blocks until every closed session is torn down. Call before the process exits: FreeRDP's
    threads use global state (WLog) that exit() destroys under them."""
    with _teardowns_lock:
        threads = list(_teardowns)
    deadline = GLib.get_monotonic_time() + int(timeout * 1_000_000)
    for thread in threads:
        remaining = (deadline - GLib.get_monotonic_time()) / 1_000_000
        thread.join(max(remaining, 0))
    with _teardowns_lock:
        return not _teardowns


class RDPSession(GObject.Object):
    """One RDP connection. All signals are emitted on the GTK main loop, in order."""

    __gsignals__ = {
        "connected": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # The connection dropped; attempt n (1, 2, …) to get the same Windows session back starts.
        "reconnecting": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        # Back in the same Windows session; every remote key is up again.
        "reconnected": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # (DisconnectReason, FreeRDP error code, technical detail)
        "disconnected": (GObject.SignalFlags.RUN_FIRST, None, (object, int, str)),
        # New pixels; read them with acquire_frame(). Fires once until the next acquire.
        "frame-ready": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "desktop-resized": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
        "pointer": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, config: SessionConfig):
        super().__init__()
        self.config = config
        self.state = SessionState.IDLE
        self.disconnect_reason: DisconnectReason | None = None
        self.desktop_size = (config.width, config.height)
        self.clipboard = RemoteClipboard()
        #: (ServerCertificate, reply) -> None, called on the main loop; reply exactly once.
        #: None accepts once (and logs).
        self.certificate_handler: Callable[[ServerCertificate, Callable[[CertificateDecision], None]], None] | None = None
        self._gate = CertificateGate(config.trusted_fingerprints)
        self._closed = False
        self._teardown_started = False
        self._callbacks = self._make_callbacks()

        os.makedirs(config.state_directory, exist_ok=True)
        drives = (bridge.WBDrive * max(len(config.drives), 1))()
        for index, (name, path) in enumerate(config.drives):
            drives[index].name = bridge.encode(name.replace("/", "_").replace("\\", "_"))
            drives[index].path = bridge.encode(path)
        raw = bridge.WBSessionConfig(
            host=bridge.encode(config.host), port=config.port, username=bridge.encode(config.username),
            domain=bridge.encode(config.domain), password=bridge.encode(config.password),
            width=config.width, height=config.height, desktopScaleFactor=config.desktop_scale,
            deviceScaleFactor=config.device_scale, keyboardLayout=config.keyboard_layout,
            ignoreCertificate=config.ignore_certificate, disableNLA=not config.nla,
            consoleSession=config.console_session, alternateShell=bridge.encode(config.alternate_shell),
            workingDirectory=bridge.encode(config.working_directory),
            loadBalanceInfo=bridge.encode(config.load_balance_info), audio=int(config.audio),
            microphone=config.microphone, printers=config.printers, drives=drives,
            driveCount=len(config.drives), clipboard=config.clipboard, autoReconnect=config.auto_reconnect,
            stateDirectory=bridge.encode(config.state_directory))
        handle = bridge.lib().wb_session_create(ctypes.byref(raw), ctypes.byref(self._callbacks))
        if not handle:
            raise RuntimeError("wb_session_create failed (invalid configuration?)")
        self._handle: int = handle
        self.clipboard._attach(handle)

    # MARK: Life cycle

    @property
    def _raw(self) -> int | None:
        return None if self._closed else self._handle

    @property
    def is_connected(self) -> bool:
        return self.state is SessionState.CONNECTED

    def start(self) -> None:
        """Starts connecting (named start because GObject already has connect())."""
        if self.state is not SessionState.IDLE or self._closed:
            return
        ok = bridge.lib().wb_session_connect(self._handle)
        self.state = SessionState.CONNECTING if ok else SessionState.DISCONNECTED

    def disconnect_session(self) -> None:
        """Ends the session; "disconnected" follows."""
        self._gate.close()
        if self._raw:
            bridge.lib().wb_session_disconnect(self._raw)

    def close(self) -> None:
        """Disconnects and frees the session in the background. Idempotent; every other call is a
        no-op afterwards and no more signals are emitted."""
        self._closed = True
        self._gate.close()
        self.clipboard._detach()
        self.clipboard.delegate = None
        if self._teardown_started:
            return
        self._teardown_started = True
        handle, callbacks = self._handle, self._callbacks

        def teardown() -> None:
            bridge.lib().wb_session_destroy(handle)
            del callbacks_keepalive[:]  # the bridge calls nothing anymore
            with _teardowns_lock:
                _teardowns.discard(threading.current_thread())

        callbacks_keepalive = [callbacks, self]
        thread = threading.Thread(target=teardown, name="weitblick-teardown", daemon=True)
        with _teardowns_lock:
            _teardowns.add(thread)
        thread.start()

    # MARK: Display

    def set_resolution(self, width: int, height: int, desktop_scale: int = 100, device_scale: int = 100) -> None:
        if self._raw:
            bridge.lib().wb_session_set_resolution(self._raw, width, height, desktop_scale, device_scale)

    def acquire_frame(self) -> Frame | None:
        """Copies the framebuffer and takes the dirty rects; re-arms "frame-ready"."""
        return self._read_frame(bridge.lib().wb_session_framebuffer_acquire, with_dirty=True)

    def peek_frame(self) -> Frame | None:
        """Copies the framebuffer without touching the dirty rects or "frame-ready" (agent screenshots)."""
        return self._read_frame(bridge.lib().wb_session_framebuffer_peek, with_dirty=False)

    def _read_frame(self, function, with_dirty: bool) -> Frame | None:
        raw = self._raw
        if not raw:
            return None
        fb = bridge.WBFramebuffer()
        if not function(raw, ctypes.byref(fb)):
            return None
        try:
            if not fb.pixels:
                return None
            data = ctypes.string_at(fb.pixels, fb.stride * fb.height)
            dirty = []
            if with_dirty:
                for index in range(fb.dirtyRectCount):
                    r = fb.dirtyRects[index]
                    dirty.append((r.x, r.y, r.width, r.height))
            return Frame(data=data, width=fb.width, height=fb.height, stride=fb.stride, dirty=dirty)
        finally:
            bridge.lib().wb_session_framebuffer_release(raw)

    # MARK: Keyboard

    def send_scancode(self, code: int, extended: bool, down: bool) -> None:
        if self._raw:
            bridge.lib().wb_session_send_scancode(self._raw, code, extended, down)

    def send_pause(self) -> None:
        if self._raw:
            bridge.lib().wb_session_send_pause(self._raw)

    def send_unicode_unit(self, code_unit: int, down: bool) -> None:
        if self._raw:
            bridge.lib().wb_session_send_unicode(self._raw, code_unit, down)

    def send_text(self, text: str) -> None:
        """Types text as Unicode key events (layout independent); no special handling of \\n or \\t."""
        for char in text:
            units = char.encode("utf-16-le")
            for index in range(0, len(units), 2):
                unit = int.from_bytes(units[index:index + 2], "little")
                self.send_unicode_unit(unit, True)
                self.send_unicode_unit(unit, False)

    def send_sync(self, caps_lock: bool, num_lock: bool, scroll_lock: bool) -> None:
        if self._raw:
            bridge.lib().wb_session_send_sync(self._raw, caps_lock, num_lock, scroll_lock)

    def release_all_keys(self) -> None:
        if self._raw:
            bridge.lib().wb_session_release_all_keys(self._raw)

    # MARK: Mouse (remote pixels)

    def send_mouse_move(self, x: int, y: int) -> None:
        if self._raw:
            bridge.lib().wb_session_send_mouse_move(self._raw, int(x), int(y))

    def send_mouse_button(self, button: MouseButton, down: bool, x: int, y: int) -> None:
        if self._raw:
            bridge.lib().wb_session_send_mouse_button(self._raw, int(button), down, int(x), int(y))

    def send_mouse_wheel(self, vertical: int, horizontal: int, x: int, y: int) -> None:
        """Windows wheel units (120 = one notch); positive vertical scrolls up, positive horizontal right."""
        if self._raw:
            bridge.lib().wb_session_send_mouse_wheel(self._raw, int(vertical), int(horizontal), int(x), int(y))

    # MARK: Bridge callbacks (FreeRDP threads)

    def _post(self, handler: Callable, *args) -> None:
        def run() -> bool:
            if not self._closed:
                try:
                    handler(*args)
                except Exception:  # noqa: BLE001 - never let one event kill the main loop
                    log.exception("session event handler failed")
            return GLib.SOURCE_REMOVE
        GLib.idle_add(run, priority=GLib.PRIORITY_DEFAULT)

    def _make_callbacks(self) -> bridge.WBCallbacks:
        clip = self.clipboard

        def connected(_ud):
            self._post(self._on_connected)

        def disconnected(_ud, reason, code, detail):
            text = detail.decode("utf-8", "replace") if detail else ""
            self._post(self._on_disconnected, DisconnectReason.from_raw(reason), code, text)

        def reconnecting(_ud, attempt):
            self._post(self._on_reconnecting, attempt)

        def reconnected(_ud):
            self._post(self._on_reconnected)

        def frame_ready(_ud):
            self._post(self.emit, "frame-ready")

        def desktop_resized(_ud, width, height):
            self._post(self._on_desktop_resized, width, height)

        def pointer_new(_ud, pointer_id, bgra, width, height, hot_x, hot_y):
            image = PointerImage(pointer_id, width, height, hot_x, hot_y, ctypes.string_at(bgra, width * height * 4))
            self._post(self.emit, "pointer", PointerEvent("new", id=pointer_id, image=image))

        def pointer_free(_ud, pointer_id):
            self._post(self.emit, "pointer", PointerEvent("free", id=pointer_id))

        def pointer_set(_ud, pointer_id):
            self._post(self.emit, "pointer", PointerEvent("set", id=pointer_id))

        def pointer_null(_ud):
            self._post(self.emit, "pointer", PointerEvent("hidden"))

        def pointer_default(_ud):
            self._post(self.emit, "pointer", PointerEvent("default"))

        def pointer_position(_ud, x, y):
            self._post(self.emit, "pointer", PointerEvent("moved", x=x, y=y))

        def verify_certificate(_ud, info_pointer):
            info = info_pointer.contents
            text = lambda value: value.decode("utf-8", "replace") if value else ""  # noqa: E731
            certificate = ServerCertificate(
                host=text(info.host), port=info.port, common_name=text(info.commonName),
                subject=text(info.subject), issuer=text(info.issuer), fingerprint=text(info.fingerprint),
                hostname_mismatch=bool(info.hostnameMismatch), changed=self._gate.trusts_other_certificates)
            return self._gate.decide(certificate, self._ask_certificate)

        def clipboard_ready(_ud):
            self._post(clip._notify, "remote_clipboard_ready")

        def clipboard_announced(_ud, accepted):
            self._post(clip._notify, "remote_clipboard_announced", bool(accepted))

        def clipboard_formats(_ud, formats, count):
            listed = [RemoteClipboardFormat(formats[i].id, formats[i].name.decode("utf-8", "replace")
                                            if formats[i].name else None) for i in range(count)]
            self._post(clip._notify, "remote_clipboard_formats", listed)

        def clipboard_data_requested(_ud, format_id):
            self._post(clip._notify, "remote_clipboard_data_requested", format_id)

        def clipboard_data_received(_ud, data, size):
            payload = ctypes.string_at(data, size) if data else None
            self._post(clip._notify, "remote_clipboard_data_received", payload)

        def clipboard_file_requested(_ud, stream_id, file_index, size_only, offset, length):
            request = RemoteFileRequest(stream_id, file_index, bool(size_only), offset, length)
            self._post(clip._notify, "remote_clipboard_file_requested", request)

        def clipboard_file_received(_ud, stream_id, data, size):
            payload = ctypes.string_at(data, size) if data else None
            self._post(clip._notify, "remote_clipboard_file_received", stream_id, payload)

        def clipboard_closed(_ud):
            self._post(clip._notify, "remote_clipboard_closed")

        return bridge.WBCallbacks(
            userData=None,
            connected=bridge.CB_VOID(connected),
            disconnected=bridge.CB_DISCONNECTED(disconnected),
            reconnecting=bridge.CB_U32(reconnecting),
            reconnected=bridge.CB_VOID(reconnected),
            frameReady=bridge.CB_VOID(frame_ready),
            desktopResized=bridge.CB_U32_U32(desktop_resized),
            pointerNew=bridge.CB_POINTER_NEW(pointer_new),
            pointerFree=bridge.CB_U64(pointer_free),
            pointerSet=bridge.CB_U64(pointer_set),
            pointerSetNull=bridge.CB_VOID(pointer_null),
            pointerSetDefault=bridge.CB_VOID(pointer_default),
            pointerPosition=bridge.CB_U32_U32(pointer_position),
            verifyCertificate=bridge.CB_VERIFY_CERTIFICATE(verify_certificate),
            clipboardReady=bridge.CB_VOID(clipboard_ready),
            clipboardAnnounced=bridge.CB_BOOL(clipboard_announced),
            clipboardRemoteFormats=bridge.CB_FORMATS(clipboard_formats),
            clipboardDataRequested=bridge.CB_U32(clipboard_data_requested),
            clipboardDataReceived=bridge.CB_DATA(clipboard_data_received),
            clipboardFileRequested=bridge.CB_FILE_REQUESTED(clipboard_file_requested),
            clipboardFileReceived=bridge.CB_FILE_RECEIVED(clipboard_file_received),
            clipboardClosed=bridge.CB_VOID(clipboard_closed),
        )

    def _ask_certificate(self, certificate: ServerCertificate, reply: Callable[[CertificateDecision], None]) -> None:
        """Runs on the FreeRDP thread; the question itself goes to the main loop."""
        def ask() -> bool:
            handler = self.certificate_handler
            if self._closed:
                reply(CertificateDecision.REJECT)
            elif handler is None:
                log.info("Accepting certificate of %s:%d (%s) sha256 %s", certificate.host, certificate.port,
                         certificate.subject, certificate.fingerprint)
                reply(CertificateDecision.ACCEPT_ONCE)
            else:
                handler(certificate, reply)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(ask)

    # MARK: Main-loop handlers

    def _on_connected(self) -> None:
        self.state = SessionState.CONNECTED
        self.emit("connected")

    def _on_reconnecting(self, attempt: int) -> None:
        self.state = SessionState.RECONNECTING
        self.emit("reconnecting", attempt)

    def _on_reconnected(self) -> None:
        self.state = SessionState.CONNECTED
        self.emit("reconnected")

    def _on_disconnected(self, reason: DisconnectReason, code: int, detail: str) -> None:
        self.state = SessionState.DISCONNECTED
        self.disconnect_reason = reason
        if reason is not DisconnectReason.REQUESTED:
            log.info("Session ended: %s – %s", reason.name, detail)
        self.emit("disconnected", reason, code, detail)

    def _on_desktop_resized(self, width: int, height: int) -> None:
        self.desktop_size = (width, height)
        self.emit("desktop-resized", width, height)
