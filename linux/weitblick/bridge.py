"""ctypes bindings for libweitblick-bridge.so (Sources/WeitblickBridge/include/WeitblickBridge.h).

Mirrors the C header one to one; the Pythonic wrapper is weitblick.rdp.session. Callbacks fire on
FreeRDP threads: ctypes takes the GIL for them, and the wrapper hands everything to the GTK main
loop. Every bridge call releases the GIL while it runs, so a callback can never deadlock against
a main-thread call that waits for a FreeRDP lock.
"""

import ctypes
from ctypes import (CFUNCTYPE, POINTER, Structure, c_bool, c_char_p, c_int, c_int32, c_size_t,
                    c_uint16, c_uint32, c_uint64, c_void_p)
from enum import IntEnum

from . import paths


class AudioMode(IntEnum):
    LOCAL = 0
    REMOTE = 1
    OFF = 2


class MouseButton(IntEnum):
    LEFT = 0
    RIGHT = 1
    MIDDLE = 2
    X1 = 3  # back
    X2 = 4  # forward


class RawDisconnectReason(IntEnum):
    REQUESTED = 0
    HOST_UNREACHABLE = 1
    LOGON_FAILED = 2
    MISSING_CREDENTIALS = 3
    ACCOUNT_LOCKED = 4
    ACCOUNT_RESTRICTED = 5
    PASSWORD_EXPIRED = 6
    ACCESS_DENIED = 7
    CERTIFICATE_REJECTED = 8
    SECURITY_FAILED = 9
    NLA_REQUIRED = 10
    SERVER_ENDED = 11
    TAKEN_OVER = 12
    LOGGED_OFF = 13
    TIMEOUT = 14
    CONNECTION_LOST = 15
    OTHER = 16


class WBDrive(Structure):
    _fields_ = [("name", c_char_p), ("path", c_char_p)]


class WBSessionConfig(Structure):
    _fields_ = [
        ("host", c_char_p),
        ("port", c_uint16),
        ("username", c_char_p),
        ("domain", c_char_p),
        ("password", c_char_p),
        ("width", c_uint32),
        ("height", c_uint32),
        ("desktopScaleFactor", c_uint32),
        ("deviceScaleFactor", c_uint32),
        ("keyboardLayout", c_uint32),
        ("ignoreCertificate", c_bool),
        ("disableNLA", c_bool),
        ("consoleSession", c_bool),
        ("alternateShell", c_char_p),
        ("workingDirectory", c_char_p),
        ("loadBalanceInfo", c_char_p),
        ("audio", c_int),
        ("microphone", c_bool),
        ("printers", c_bool),
        ("drives", POINTER(WBDrive)),
        ("driveCount", c_size_t),
        ("clipboard", c_bool),
        ("autoReconnect", c_bool),
        ("stateDirectory", c_char_p),
    ]


class WBRect(Structure):
    _fields_ = [("x", c_int32), ("y", c_int32), ("width", c_int32), ("height", c_int32)]


class WBClipboardFormat(Structure):
    _fields_ = [("id", c_uint32), ("name", c_char_p)]


class WBCertificateInfo(Structure):
    _fields_ = [
        ("host", c_char_p),
        ("port", c_uint16),
        ("commonName", c_char_p),
        ("subject", c_char_p),
        ("issuer", c_char_p),
        ("fingerprint", c_char_p),
        ("hostnameMismatch", c_bool),
    ]


class WBFramebuffer(Structure):
    _fields_ = [
        ("pixels", c_void_p),
        ("width", c_uint32),
        ("height", c_uint32),
        ("stride", c_uint32),
        ("dirtyRects", POINTER(WBRect)),
        ("dirtyRectCount", c_size_t),
    ]


# Callback types (the first argument is the unused userData; every session binds its own closures).
CB_VOID = CFUNCTYPE(None, c_void_p)
CB_DISCONNECTED = CFUNCTYPE(None, c_void_p, c_int, c_uint32, c_char_p)
CB_U32 = CFUNCTYPE(None, c_void_p, c_uint32)
CB_U32_U32 = CFUNCTYPE(None, c_void_p, c_uint32, c_uint32)
CB_POINTER_NEW = CFUNCTYPE(None, c_void_p, c_uint64, c_void_p, c_uint32, c_uint32, c_uint32, c_uint32)
CB_U64 = CFUNCTYPE(None, c_void_p, c_uint64)
CB_VERIFY_CERTIFICATE = CFUNCTYPE(c_bool, c_void_p, POINTER(WBCertificateInfo))
CB_BOOL = CFUNCTYPE(None, c_void_p, c_bool)
CB_FORMATS = CFUNCTYPE(None, c_void_p, POINTER(WBClipboardFormat), c_size_t)
CB_DATA = CFUNCTYPE(None, c_void_p, c_void_p, c_size_t)
CB_FILE_REQUESTED = CFUNCTYPE(None, c_void_p, c_uint32, c_uint32, c_bool, c_uint64, c_uint32)
CB_FILE_RECEIVED = CFUNCTYPE(None, c_void_p, c_uint32, c_void_p, c_size_t)


class WBCallbacks(Structure):
    _fields_ = [
        ("userData", c_void_p),
        ("connected", CB_VOID),
        ("disconnected", CB_DISCONNECTED),
        ("reconnecting", CB_U32),
        ("reconnected", CB_VOID),
        ("frameReady", CB_VOID),
        ("desktopResized", CB_U32_U32),
        ("pointerNew", CB_POINTER_NEW),
        ("pointerFree", CB_U64),
        ("pointerSet", CB_U64),
        ("pointerSetNull", CB_VOID),
        ("pointerSetDefault", CB_VOID),
        ("pointerPosition", CB_U32_U32),
        ("verifyCertificate", CB_VERIFY_CERTIFICATE),
        ("clipboardReady", CB_VOID),
        ("clipboardAnnounced", CB_BOOL),
        ("clipboardRemoteFormats", CB_FORMATS),
        ("clipboardDataRequested", CB_U32),
        ("clipboardDataReceived", CB_DATA),
        ("clipboardFileRequested", CB_FILE_REQUESTED),
        ("clipboardFileReceived", CB_FILE_RECEIVED),
        ("clipboardClosed", CB_VOID),
    ]


_SIGNATURES = {
    "wb_session_create": (c_void_p, [POINTER(WBSessionConfig), POINTER(WBCallbacks)]),
    "wb_session_connect": (c_bool, [c_void_p]),
    "wb_session_disconnect": (None, [c_void_p]),
    "wb_session_destroy": (None, [c_void_p]),
    "wb_session_framebuffer_acquire": (c_bool, [c_void_p, POINTER(WBFramebuffer)]),
    "wb_session_framebuffer_peek": (c_bool, [c_void_p, POINTER(WBFramebuffer)]),
    "wb_session_framebuffer_release": (None, [c_void_p]),
    "wb_session_set_resolution": (None, [c_void_p, c_uint32, c_uint32, c_uint32, c_uint32]),
    "wb_session_send_scancode": (None, [c_void_p, c_uint16, c_bool, c_bool]),
    "wb_session_send_pause": (None, [c_void_p]),
    "wb_session_send_unicode": (None, [c_void_p, c_uint16, c_bool]),
    "wb_session_send_sync": (None, [c_void_p, c_bool, c_bool, c_bool]),
    "wb_session_release_all_keys": (None, [c_void_p]),
    "wb_session_send_mouse_move": (None, [c_void_p, c_int32, c_int32]),
    "wb_session_send_mouse_button": (None, [c_void_p, c_int, c_bool, c_int32, c_int32]),
    "wb_session_send_mouse_wheel": (None, [c_void_p, c_int32, c_int32, c_int32, c_int32]),
    "wb_session_clipboard_announce": (c_bool, [c_void_p, POINTER(WBClipboardFormat), c_size_t]),
    "wb_session_clipboard_request": (c_bool, [c_void_p, c_uint32]),
    "wb_session_clipboard_respond": (c_bool, [c_void_p, c_bool, c_void_p, c_size_t]),
    "wb_session_clipboard_file_request": (c_bool, [c_void_p, c_uint32, c_uint32, c_bool, c_uint64, c_uint32]),
    "wb_session_clipboard_file_respond": (c_bool, [c_void_p, c_uint32, c_bool, c_void_p, c_size_t]),
}

_lib = None


class BridgeUnavailable(RuntimeError):
    pass


def lib():
    """The loaded bridge library (loaded on first use)."""
    global _lib
    if _lib is None:
        path = paths.bridge_library()
        try:
            loaded = ctypes.CDLL(str(path))
        except OSError as error:
            raise BridgeUnavailable(
                f"Can't load {path}: {error}. Build it with 'make' (needs freerdp3-dev or 'make headers')."
            ) from error
        for name, (restype, argtypes) in _SIGNATURES.items():
            function = getattr(loaded, name)
            function.restype = restype
            function.argtypes = argtypes
        _lib = loaded
    return _lib


def encode(text: str | None) -> bytes | None:
    return None if text is None else text.encode("utf-8")
