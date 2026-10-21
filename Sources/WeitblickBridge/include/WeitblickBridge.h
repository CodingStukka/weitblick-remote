// WeitblickBridge: a small C API around libfreerdp for the Weitblick Remote app.
//
// Threading: every session runs its own event-loop thread. Callbacks fire on that
// thread and must return quickly (no blocking on the main thread). All other
// functions may be called from any thread; input functions are no-ops unless the
// session is connected.
#ifndef WEITBLICK_BRIDGE_H
#define WEITBLICK_BRIDGE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct WBSession WBSession;

typedef enum {
    WBAudioLocal = 0,  ///< played on the Mac (rdpsnd, macOS backend)
    WBAudioRemote = 1, ///< left on the remote computer
    WBAudioOff = 2,
} WBAudioMode;

/// A local folder shown in the session as \\tsclient\<name>.
typedef struct {
    const char *name;
    const char *path;
} WBDrive;

typedef struct {
    const char *host;
    uint16_t port;
    const char *username;
    const char *domain;
    const char *password;
    /// Initial desktop size in remote pixels.
    uint32_t width;
    uint32_t height;
    /// Windows DPI scale in percent (100…500), e.g. 200 for a Retina window.
    uint32_t desktopScaleFactor;
    /// 100, 140 or 180.
    uint32_t deviceScaleFactor;
    /// Windows keyboard layout id (KLID), e.g. 0x0407 for German.
    uint32_t keyboardLayout;
    bool ignoreCertificate;
    /// No Network Level Authentication (old targets): TLS, else standard RDP security; the
    /// credentials then go to the Windows logon screen inside the session.
    bool disableNLA;
    /// Connects to the console session (mstsc /admin).
    bool consoleSession;
    /// Program started instead of the desktop, and its working directory; NULL or "" for none.
    const char *alternateShell;
    const char *workingDirectory;
    /// Load-balancer routing token (.rdp "loadbalanceinfo"); NULL or "" for none.
    const char *loadBalanceInfo;
    WBAudioMode audio;
    /// Microphone redirection (audin, macOS backend).
    bool microphone;
    /// Printer redirection (CUPS printers of the Mac).
    bool printers;
    /// Drive redirection; strings are copied.
    const WBDrive *drives;
    size_t driveCount;
    /// Loads the clipboard channel (cliprdr); see the clipboard callbacks and functions.
    bool clipboard;
    /// Reconnects with backoff when the network drops (never after a user, server or credential end).
    bool autoReconnect;
    /// Directory for FreeRDP's own state (certificate and license stores). Keeps Weitblick Remote
    /// away from ~/.config/freerdp; NULL uses that default.
    const char *stateDirectory;
} WBSessionConfig;

typedef struct {
    int32_t x;
    int32_t y;
    int32_t width;
    int32_t height;
} WBRect;

/// A clipboard format. Standard Windows formats (CF_UNICODETEXT = 13, CF_DIB = 8, …) have no name;
/// registered ones ("HTML Format", "PNG", …) carry their name and an id chosen by the announcing side.
typedef struct {
    uint32_t id;
    const char *name; ///< NULL for standard formats
} WBClipboardFormat;

typedef struct {
    const char *host;
    uint16_t port;
    const char *commonName;
    const char *subject;
    const char *issuer;
    /// SHA-256 fingerprint as colon-separated hex.
    const char *fingerprint;
    /// The certificate does not name the host we connected to.
    bool hostnameMismatch;
} WBCertificateInfo;

/// Why a session ended; the mapping from FreeRDP errors is in wb_disconnect.c.
typedef enum {
    WBDisconnectRequested = 0, ///< wb_session_disconnect
    WBDisconnectHostUnreachable,
    WBDisconnectLogonFailed, ///< wrong user name or password
    WBDisconnectMissingCredentials,
    WBDisconnectAccountLocked,
    WBDisconnectAccountRestricted, ///< disabled, expired, or not allowed to log on here
    WBDisconnectPasswordExpired,
    WBDisconnectAccessDenied,
    WBDisconnectCertificateRejected,
    WBDisconnectSecurityFailed, ///< TLS or security negotiation
    WBDisconnectNLARequired,    ///< NLA was off, but the server requires it
    WBDisconnectServerEnded,    ///< the server or an administrator ended the session
    WBDisconnectTakenOver,      ///< another connection took the session
    WBDisconnectLoggedOff,      ///< the user logged off or disconnected in Windows
    WBDisconnectTimeout,
    WBDisconnectConnectionLost, ///< network drop (after reconnect attempts, if enabled)
    WBDisconnectOther,
} WBDisconnectReason;

typedef struct {
    void *userData;
    /// Connection is up; input may be sent from now on.
    void (*connected)(void *userData);
    /// Session ended. `errorCode` is the FreeRDP last error (0 when requested); `detail` is a
    /// technical description for logs, valid only during the call.
    void (*disconnected)(void *userData, WBDisconnectReason reason, uint32_t errorCode, const char *detail);
    /// The connection dropped; reconnect attempt `attempt` (1, 2, …) starts. Input is off until
    /// `reconnected`.
    void (*reconnecting)(void *userData, uint32_t attempt);
    /// The session is back (same remote session); input works again, all keys are up.
    void (*reconnected)(void *userData);
    /// New pixels are available. Coalesced: fires once until the framebuffer is acquired.
    void (*frameReady)(void *userData);
    /// The remote desktop changed size (pixels).
    void (*desktopResized)(void *userData, uint32_t width, uint32_t height);
    /// A new server cursor. `bgra` is BGRA32 with straight alpha, top-down, `width * 4` bytes
    /// per row, valid only during the call.
    void (*pointerNew)(void *userData, uint64_t pointerId, const uint8_t *bgra, uint32_t width,
                       uint32_t height, uint32_t hotspotX, uint32_t hotspotY);
    void (*pointerFree)(void *userData, uint64_t pointerId);
    void (*pointerSet)(void *userData, uint64_t pointerId);
    /// The server hides the cursor.
    void (*pointerSetNull)(void *userData);
    /// The server wants the system default cursor.
    void (*pointerSetDefault)(void *userData);
    /// The server moved the cursor (remote pixels).
    void (*pointerPosition)(void *userData, uint32_t x, uint32_t y);
    /// An unknown certificate (no valid chain). Return true to accept it; FreeRDP never stores it.
    /// May block (the connection waits), but must return when the session is closed. NULL accepts.
    bool (*verifyCertificate)(void *userData, const WBCertificateInfo *info);

    // Clipboard (only with WBSessionConfig.clipboard). These fire on the clipboard channel thread.
    /// The channel is up: announce the local clipboard now (wb_session_clipboard_announce).
    void (*clipboardReady)(void *userData);
    /// The server took a format list we announced (false: it rejected it).
    void (*clipboardAnnounced)(void *userData, bool accepted);
    /// The server's clipboard changed. `formats` is valid only during the call.
    void (*clipboardRemoteFormats)(void *userData, const WBClipboardFormat *formats, size_t count);
    /// The server wants local clipboard data in `formatId` (an id we announced). Answer once with
    /// wb_session_clipboard_respond, from any thread and at any later time.
    void (*clipboardDataRequested)(void *userData, uint32_t formatId);
    /// Answer to wb_session_clipboard_request; `data` is NULL if the server failed. Valid only
    /// during the call.
    void (*clipboardDataReceived)(void *userData, const uint8_t *data, size_t size);
    /// The server wants part of a file we announced (FileGroupDescriptorW): its size (`sizeOnly`,
    /// answer 8 bytes little endian) or `length` bytes at `offset`. Answer once with
    /// wb_session_clipboard_file_respond, from any thread.
    void (*clipboardFileRequested)(void *userData, uint32_t streamId, uint32_t fileIndex, bool sizeOnly,
                                   uint64_t offset, uint32_t length);
    /// Answer to wb_session_clipboard_file_request; `data` is NULL if the server failed. Valid
    /// only during the call.
    void (*clipboardFileReceived)(void *userData, uint32_t streamId, const uint8_t *data, size_t size);
    /// The clipboard channel went down (session end or reconnect); requests in flight are lost.
    void (*clipboardClosed)(void *userData);
} WBCallbacks;

/// Locked view of the framebuffer, see wb_session_framebuffer_acquire.
typedef struct {
    const uint8_t *pixels; ///< BGRA32, top-down
    uint32_t width;
    uint32_t height;
    uint32_t stride; ///< bytes per row
    /// Regions changed since the previous acquire, clipped to the framebuffer.
    const WBRect *dirtyRects;
    size_t dirtyRectCount;
} WBFramebuffer;

typedef enum {
    WBMouseButtonLeft = 0,
    WBMouseButtonRight = 1,
    WBMouseButtonMiddle = 2,
    WBMouseButtonX1 = 3, ///< "back"
    WBMouseButtonX2 = 4, ///< "forward"
} WBMouseButton;

/// Creates a session; nothing happens on the network until wb_session_connect.
/// Strings are copied. Returns NULL on invalid config or allocation failure.
WBSession *wb_session_create(const WBSessionConfig *config,
                                     const WBCallbacks *callbacks);

/// Starts the event-loop thread, which connects and then runs the session.
bool wb_session_connect(WBSession *session);

/// Asks the session to end (also aborts a connect in progress). Returns immediately;
/// `disconnected` fires once the session is down.
void wb_session_disconnect(WBSession *session);

/// Disconnects if needed, joins the event-loop thread and frees everything.
/// No callback fires after this returns. Must not be called from a callback.
void wb_session_destroy(WBSession *session);

/// Locks the framebuffer for reading and hands over the dirty rects collected since the
/// previous call. Returns false (and nothing is locked) if there is no framebuffer yet.
/// Keep the lock short: decoding waits for it.
bool wb_session_framebuffer_acquire(WBSession *session, WBFramebuffer *out);
/// Locks the framebuffer for reading like wb_session_framebuffer_acquire, but leaves the dirty
/// rects and the frame signal to the presenter (dirtyRectCount is 0). For other readers, such as
/// agent screenshots. Release with wb_session_framebuffer_release.
bool wb_session_framebuffer_peek(WBSession *session, WBFramebuffer *out);
void wb_session_framebuffer_release(WBSession *session);

/// Requests a new remote desktop size via the display-control channel. Width is rounded
/// down to an even value, both are clamped to 200…8192. The latest request wins; it is sent
/// once the channel is ready and no earlier resize is still in flight. Debounce in the caller.
void wb_session_set_resolution(WBSession *session, uint32_t width, uint32_t height,
                                   uint32_t desktopScaleFactor, uint32_t deviceScaleFactor);

// Keyboard. Scancodes are PC/AT set 1 make codes (0x01…0x7F) plus the extended (E0) flag; (0x46,
// extended) is Break (Ctrl+Pause). A key-down for a key that is already down is sent as a repeat.
// Key-ups for keys that are not down are dropped.
void wb_session_send_scancode(WBSession *session, uint16_t code, bool extended, bool down);
/// The Pause key: its whole E1 make/break sequence (Pause has no key-up of its own).
void wb_session_send_pause(WBSession *session);
/// UTF-16 code unit; characters outside the BMP need two calls (surrogate pair).
void wb_session_send_unicode(WBSession *session, uint16_t codeUnit, bool down);
/// Sets the remote lock-key state (synchronize event).
void wb_session_send_sync(WBSession *session, bool capsLock, bool numLock, bool scrollLock);
/// Sends key-up for every scancode currently down.
void wb_session_release_all_keys(WBSession *session);

// Mouse. Coordinates are remote pixels and get clamped to the desktop.
void wb_session_send_mouse_move(WBSession *session, int32_t x, int32_t y);
void wb_session_send_mouse_button(WBSession *session, WBMouseButton button, bool down,
                                      int32_t x, int32_t y);
/// Wheel deltas in Windows units (120 = one notch). Positive vertical scrolls up (away from
/// the user), positive horizontal scrolls right. Large deltas are split into several events.
void wb_session_send_mouse_wheel(WBSession *session, int32_t vertical, int32_t horizontal,
                                     int32_t x, int32_t y);

// Clipboard. All return false if the clipboard channel is not up. Requests and responses pair up in
// order (the protocol has no ids): keep at most one request outstanding.
/// Announces the local clipboard formats. Names are copied.
bool wb_session_clipboard_announce(WBSession *session, const WBClipboardFormat *formats, size_t count);
/// Asks the server for its clipboard data in `formatId` (a server format id); the answer arrives
/// through clipboardDataReceived.
bool wb_session_clipboard_request(WBSession *session, uint32_t formatId);
/// Answers clipboardDataRequested; `ok` false reports failure. The data is copied.
bool wb_session_clipboard_respond(WBSession *session, bool ok, const uint8_t *data, size_t size);

// Files over the clipboard (MS-RDPECLIP file streams). Requests carry a caller-chosen stream id that
// the answer repeats, so several may be in flight.
/// Asks the server for the size (`sizeOnly`) or `length` bytes at `offset` of file `fileIndex` in
/// the server's current FileGroupDescriptorW; the answer arrives through clipboardFileReceived.
bool wb_session_clipboard_file_request(WBSession *session, uint32_t streamId, uint32_t fileIndex,
                                           bool sizeOnly, uint64_t offset, uint32_t length);
/// Answers clipboardFileRequested; `ok` false reports failure. The data is copied.
bool wb_session_clipboard_file_respond(WBSession *session, uint32_t streamId, bool ok,
                                           const uint8_t *data, size_t size);

#ifdef __cplusplus
}
#endif

#endif
