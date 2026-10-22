// Turns a WBSessionConfig into FreeRDP settings (redirection: wb_redirection.c).
#include "wb_internal.h"

#include <string.h>

#include <freerdp/constants.h>

#define TLS_VERSION_1_0 0x0301 // OpenSSL's TLS1_VERSION

// The client OS the server sees: the macOS app or the Linux port (linux/), which builds this bridge too.
#ifdef __APPLE__
#define WB_OS_MAJOR_TYPE OSMAJORTYPE_MACINTOSH
#define WB_OS_MINOR_TYPE OSMINORTYPE_MACINTOSH
#else
#define WB_OS_MAJOR_TYPE OSMAJORTYPE_UNIX
#define WB_OS_MINOR_TYPE OSMINORTYPE_NATIVE_XSERVER
#endif

// Old Windows (7 without updates, Embedded/IoT) speak only TLS 1.0 with SHA-1, which OpenSSL 3
// refuses unless the minimum version and the security level allow it. The client still offers its
// best version, the handshake's Finished messages protect that choice against tampering, and
// certificates are pinned by fingerprint. Standard RDP security stays as the last fallback.
static bool apply_security(rdpSettings *s, const WBSessionConfig *c) {
    // Both NLA variants: CredSSP (HYBRID) and with early user authorization (HYBRID_EX).
    return freerdp_settings_set_bool(s, FreeRDP_NlaSecurity, !c->disableNLA) &&
           freerdp_settings_set_bool(s, FreeRDP_ExtSecurity, !c->disableNLA) &&
           freerdp_settings_set_bool(s, FreeRDP_TlsSecurity, TRUE) &&
           freerdp_settings_set_bool(s, FreeRDP_RdpSecurity, TRUE) &&
           freerdp_settings_set_bool(s, FreeRDP_NegotiateSecurityLayer, TRUE) &&
           freerdp_settings_set_uint16(s, FreeRDP_TLSMinVersion, TLS_VERSION_1_0) &&
           freerdp_settings_set_uint32(s, FreeRDP_TlsSecLevel, 0) &&
           freerdp_settings_set_bool(s, FreeRDP_IgnoreCertificate, c->ignoreCertificate) &&
           freerdp_settings_set_bool(s, FreeRDP_CertificateCallbackPreferPEM, FALSE);
}

static bool apply_session_options(rdpSettings *s, const WBSessionConfig *c) {
    const char *routing = c->loadBalanceInfo;
    if (routing && routing[0] && !freerdp_settings_set_pointer_len(s, FreeRDP_LoadBalanceInfo, routing, strlen(routing)))
        return false;
    return freerdp_settings_set_bool(s, FreeRDP_ConsoleSession, c->consoleSession) &&
           freerdp_settings_set_string(s, FreeRDP_AlternateShell, c->alternateShell) &&
           freerdp_settings_set_string(s, FreeRDP_ShellWorkingDirectory, c->workingDirectory);
}

bool wb_settings_apply(rdpSettings *s, const WBSessionConfig *c) {
    const uint32_t width = wb_clamp_u32(c->width, 200, 8192) & ~1u;
    const uint32_t height = wb_clamp_u32(c->height, 200, 8192);
    if (c->stateDirectory && !freerdp_settings_set_string(s, FreeRDP_ConfigPath, c->stateDirectory))
        return false;
    return freerdp_settings_set_string(s, FreeRDP_ServerHostname, c->host) &&
           freerdp_settings_set_uint32(s, FreeRDP_ServerPort, c->port ? c->port : 3389) &&
           freerdp_settings_set_string(s, FreeRDP_Username, c->username) &&
           freerdp_settings_set_string(s, FreeRDP_Password, c->password) &&
           freerdp_settings_set_string(s, FreeRDP_Domain, c->domain) &&
           apply_security(s, c) &&
           apply_session_options(s, c) &&
           freerdp_settings_set_uint32(s, FreeRDP_DesktopWidth, width) &&
           freerdp_settings_set_uint32(s, FreeRDP_DesktopHeight, height) &&
           freerdp_settings_set_uint32(s, FreeRDP_DesktopScaleFactor,
                                       wb_clamp_u32(c->desktopScaleFactor, 100, 500)) &&
           freerdp_settings_set_uint32(s, FreeRDP_DeviceScaleFactor,
                                       c->deviceScaleFactor ? c->deviceScaleFactor : 100) &&
           freerdp_settings_set_uint32(s, FreeRDP_KeyboardLayout, c->keyboardLayout) &&
           freerdp_settings_set_uint32(s, FreeRDP_ColorDepth, 32) &&
           wb_redirection_apply(s, c) &&
           freerdp_settings_set_bool(s, FreeRDP_RedirectClipboard, c->clipboard) &&
           // Reconnects run in wb_session_thread.c; the flag makes the server send its cookie.
           freerdp_settings_set_bool(s, FreeRDP_AutoReconnectionEnabled, c->autoReconnect) &&
           // Graphics pipeline with the codecs we can decode in software. No AVC: there is no
           // H.264 decoder in this build, so rdpgfx advertises AVC_DISABLED.
           freerdp_settings_set_bool(s, FreeRDP_SupportGraphicsPipeline, TRUE) &&
           freerdp_settings_set_bool(s, FreeRDP_GfxH264, FALSE) &&
           freerdp_settings_set_bool(s, FreeRDP_GfxAVC444, FALSE) &&
           freerdp_settings_set_bool(s, FreeRDP_GfxAVC444v2, FALSE) &&
           freerdp_settings_set_bool(s, FreeRDP_GfxProgressive, TRUE) &&
           freerdp_settings_set_bool(s, FreeRDP_GfxProgressiveV2, TRUE) &&
           freerdp_settings_set_bool(s, FreeRDP_GfxPlanar, TRUE) &&
           freerdp_settings_set_bool(s, FreeRDP_RemoteFxCodec, TRUE) &&
           freerdp_settings_set_bool(s, FreeRDP_SupportDisplayControl, TRUE) &&
           freerdp_settings_set_bool(s, FreeRDP_DynamicResolutionUpdate, TRUE) &&
           freerdp_set_connection_type(s, CONNECTION_TYPE_AUTODETECT) &&
           freerdp_settings_set_uint32(s, FreeRDP_OsMajorType, WB_OS_MAJOR_TYPE) &&
           freerdp_settings_set_uint32(s, FreeRDP_OsMinorType, WB_OS_MINOR_TYPE);
}
