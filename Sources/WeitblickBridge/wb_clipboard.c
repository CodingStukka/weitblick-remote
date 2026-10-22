// Clipboard redirection (MS-RDPECLIP) over FreeRDP's cliprdr channel. The bridge only moves
// format lists and data blobs; formats and conversions live in the app (Swift; Python on Linux).
//
// Server messages arrive on the channel's message thread. The API functions may be called from
// any thread: they only queue PDUs, which the session's event loop sends.
#include "wb_internal.h"

#include <stdlib.h>

#include <freerdp/client/cliprdr.h>
#include <winpr/wlog.h>

static WBSession *session_of(CliprdrClientContext *cliprdr) {
    return cliprdr ? cliprdr->custom : NULL;
}

static UINT send_client_capabilities(CliprdrClientContext *cliprdr) {
    // File streams with relative names only, 64-bit offsets. No clipboard locking: a file paste
    // fails if the copying side's clipboard changed meanwhile. The channel drops what the server
    // does not support.
    CLIPRDR_GENERAL_CAPABILITY_SET general = {
        .capabilitySetType = CB_CAPSTYPE_GENERAL,
        .capabilitySetLength = CB_CAPSTYPE_GENERAL_LEN,
        .version = CB_CAPS_VERSION_2,
        .generalFlags = CB_USE_LONG_FORMAT_NAMES | CB_STREAM_FILECLIP_ENABLED | CB_FILECLIP_NO_FILE_PATHS |
                        CB_HUGE_FILE_SUPPORT_ENABLED,
    };
    const CLIPRDR_CAPABILITIES capabilities = {
        .common = { .msgType = CB_CLIP_CAPS },
        .cCapabilitiesSets = 1,
        .capabilitySets = (CLIPRDR_CAPABILITY_SET *)&general,
    };
    return cliprdr->ClientCapabilities(cliprdr, &capabilities);
}

static UINT on_monitor_ready(CliprdrClientContext *cliprdr, const CLIPRDR_MONITOR_READY *ready) {
    (void)ready;
    WBSession *session = session_of(cliprdr);
    const UINT rc = send_client_capabilities(cliprdr);
    if (rc != CHANNEL_RC_OK)
        return rc;
    // The client must answer with its format list; the app announces the local clipboard.
    if (session->callbacks.clipboardReady)
        session->callbacks.clipboardReady(session->callbacks.userData);
    return CHANNEL_RC_OK;
}

static UINT on_server_capabilities(CliprdrClientContext *cliprdr, const CLIPRDR_CAPABILITIES *capabilities) {
    (void)cliprdr;
    (void)capabilities;
    return CHANNEL_RC_OK;
}

static UINT on_server_format_list(CliprdrClientContext *cliprdr, const CLIPRDR_FORMAT_LIST *list) {
    WBSession *session = session_of(cliprdr);
    const CLIPRDR_FORMAT_LIST_RESPONSE response = {
        .common = { .msgType = CB_FORMAT_LIST_RESPONSE, .msgFlags = CB_RESPONSE_OK },
    };
    const UINT rc = cliprdr->ClientFormatListResponse(cliprdr, &response);

    if (session->callbacks.clipboardRemoteFormats) {
        WBClipboardFormat *formats = calloc(list->numFormats ? list->numFormats : 1, sizeof(*formats));
        if (!formats)
            return CHANNEL_RC_NO_MEMORY;
        for (UINT32 i = 0; i < list->numFormats; i++) {
            formats[i].id = list->formats[i].formatId;
            formats[i].name = list->formats[i].formatName;
        }
        session->callbacks.clipboardRemoteFormats(session->callbacks.userData, formats, list->numFormats);
        free(formats);
    }
    return rc;
}

static UINT on_server_format_list_response(CliprdrClientContext *cliprdr,
                                           const CLIPRDR_FORMAT_LIST_RESPONSE *response) {
    WBSession *session = session_of(cliprdr);
    const bool accepted = (response->common.msgFlags & CB_RESPONSE_OK) != 0;
    if (!accepted)
        WLog_WARN(WB_TAG, "server rejected the clipboard format list");
    if (session->callbacks.clipboardAnnounced)
        session->callbacks.clipboardAnnounced(session->callbacks.userData, accepted);
    return CHANNEL_RC_OK;
}

static UINT on_server_format_data_request(CliprdrClientContext *cliprdr,
                                          const CLIPRDR_FORMAT_DATA_REQUEST *request) {
    WBSession *session = session_of(cliprdr);
    if (session->callbacks.clipboardDataRequested) {
        session->callbacks.clipboardDataRequested(session->callbacks.userData, request->requestedFormatId);
        return CHANNEL_RC_OK;
    }
    const CLIPRDR_FORMAT_DATA_RESPONSE failure = {
        .common = { .msgType = CB_FORMAT_DATA_RESPONSE, .msgFlags = CB_RESPONSE_FAIL },
    };
    return cliprdr->ClientFormatDataResponse(cliprdr, &failure);
}

static UINT on_server_format_data_response(CliprdrClientContext *cliprdr,
                                           const CLIPRDR_FORMAT_DATA_RESPONSE *response) {
    WBSession *session = session_of(cliprdr);
    const bool ok = (response->common.msgFlags & CB_RESPONSE_FAIL) == 0 && response->requestedFormatData;
    if (session->callbacks.clipboardDataReceived)
        session->callbacks.clipboardDataReceived(session->callbacks.userData, ok ? response->requestedFormatData : NULL,
                                                 ok ? response->common.dataLen : 0);
    return CHANNEL_RC_OK;
}

static UINT on_server_file_contents_request(CliprdrClientContext *cliprdr,
                                            const CLIPRDR_FILE_CONTENTS_REQUEST *request) {
    WBSession *session = session_of(cliprdr);
    const bool sizeOnly = (request->dwFlags & FILECONTENTS_SIZE) != 0;
    if (session->callbacks.clipboardFileRequested) {
        const uint64_t offset = ((uint64_t)request->nPositionHigh << 32) | request->nPositionLow;
        session->callbacks.clipboardFileRequested(session->callbacks.userData, request->streamId,
                                                  request->listIndex, sizeOnly, offset, request->cbRequested);
        return CHANNEL_RC_OK;
    }
    const CLIPRDR_FILE_CONTENTS_RESPONSE failure = {
        .common = { .msgType = CB_FILECONTENTS_RESPONSE, .msgFlags = CB_RESPONSE_FAIL },
        .streamId = request->streamId,
    };
    return cliprdr->ClientFileContentsResponse(cliprdr, &failure);
}

static UINT on_server_file_contents_response(CliprdrClientContext *cliprdr,
                                             const CLIPRDR_FILE_CONTENTS_RESPONSE *response) {
    static const BYTE nothing = 0; // non-NULL for an empty but successful answer
    WBSession *session = session_of(cliprdr);
    const bool ok = (response->common.msgFlags & CB_RESPONSE_FAIL) == 0 &&
                    (response->requestedData || response->cbRequested == 0);
    const BYTE *data = response->requestedData ? response->requestedData : &nothing;
    if (session->callbacks.clipboardFileReceived)
        session->callbacks.clipboardFileReceived(session->callbacks.userData, response->streamId,
                                                 ok ? data : NULL, ok ? response->cbRequested : 0);
    return CHANNEL_RC_OK;
}

static UINT on_server_lock(CliprdrClientContext *cliprdr, const CLIPRDR_LOCK_CLIPBOARD_DATA *lock) {
    (void)cliprdr;
    (void)lock;
    return CHANNEL_RC_OK;
}

static UINT on_server_unlock(CliprdrClientContext *cliprdr, const CLIPRDR_UNLOCK_CLIPBOARD_DATA *unlock) {
    (void)cliprdr;
    (void)unlock;
    return CHANNEL_RC_OK;
}

void wb_clipboard_channel_connected(WBSession *session, CliprdrClientContext *cliprdr) {
    cliprdr->custom = session;
    cliprdr->MonitorReady = on_monitor_ready;
    cliprdr->ServerCapabilities = on_server_capabilities;
    cliprdr->ServerFormatList = on_server_format_list;
    cliprdr->ServerFormatListResponse = on_server_format_list_response;
    cliprdr->ServerFormatDataRequest = on_server_format_data_request;
    cliprdr->ServerFormatDataResponse = on_server_format_data_response;
    cliprdr->ServerFileContentsRequest = on_server_file_contents_request;
    cliprdr->ServerFileContentsResponse = on_server_file_contents_response;
    cliprdr->ServerLockClipboardData = on_server_lock;
    cliprdr->ServerUnlockClipboardData = on_server_unlock;
    pthread_mutex_lock(&session->clipboardLock);
    session->cliprdr = cliprdr;
    pthread_mutex_unlock(&session->clipboardLock);
}

void wb_clipboard_channel_disconnected(WBSession *session) {
    pthread_mutex_lock(&session->clipboardLock);
    session->cliprdr = NULL;
    pthread_mutex_unlock(&session->clipboardLock);
    if (session->callbacks.clipboardClosed)
        session->callbacks.clipboardClosed(session->callbacks.userData);
}

// MARK: - API

static CliprdrClientContext *clipboard_begin(WBSession *session) {
    if (!session)
        return NULL;
    pthread_mutex_lock(&session->clipboardLock);
    if (session->cliprdr)
        return session->cliprdr;
    pthread_mutex_unlock(&session->clipboardLock);
    return NULL;
}

static void clipboard_end(WBSession *session) {
    pthread_mutex_unlock(&session->clipboardLock);
}

bool wb_session_clipboard_announce(WBSession *session, const WBClipboardFormat *formats, size_t count) {
    CliprdrClientContext *cliprdr = clipboard_begin(session);
    if (!cliprdr)
        return false;
    CLIPRDR_FORMAT *list = calloc(count ? count : 1, sizeof(*list));
    UINT rc = CHANNEL_RC_NO_MEMORY;
    if (list) {
        for (size_t i = 0; i < count; i++) {
            list[i].formatId = formats[i].id;
            list[i].formatName = (char *)formats[i].name; // copied when the PDU is built
        }
        const CLIPRDR_FORMAT_LIST formatList = {
            .common = { .msgType = CB_FORMAT_LIST },
            .numFormats = (UINT32)count,
            .formats = list,
        };
        rc = cliprdr->ClientFormatList(cliprdr, &formatList);
        free(list);
    }
    clipboard_end(session);
    return rc == CHANNEL_RC_OK;
}

bool wb_session_clipboard_request(WBSession *session, uint32_t formatId) {
    CliprdrClientContext *cliprdr = clipboard_begin(session);
    if (!cliprdr)
        return false;
    const CLIPRDR_FORMAT_DATA_REQUEST request = {
        .common = { .msgType = CB_FORMAT_DATA_REQUEST },
        .requestedFormatId = formatId,
    };
    const UINT rc = cliprdr->ClientFormatDataRequest(cliprdr, &request);
    clipboard_end(session);
    return rc == CHANNEL_RC_OK;
}

bool wb_session_clipboard_respond(WBSession *session, bool ok, const uint8_t *data, size_t size) {
    if (size > UINT32_MAX || (size > 0 && !data))
        ok = false;
    CliprdrClientContext *cliprdr = clipboard_begin(session);
    if (!cliprdr)
        return false;
    const CLIPRDR_FORMAT_DATA_RESPONSE response = {
        .common = {
            .msgType = CB_FORMAT_DATA_RESPONSE,
            .msgFlags = ok ? CB_RESPONSE_OK : CB_RESPONSE_FAIL,
            .dataLen = ok ? (UINT32)size : 0,
        },
        .requestedFormatData = ok ? data : NULL,
    };
    const UINT rc = cliprdr->ClientFormatDataResponse(cliprdr, &response);
    clipboard_end(session);
    return rc == CHANNEL_RC_OK;
}

bool wb_session_clipboard_file_request(WBSession *session, uint32_t streamId, uint32_t fileIndex,
                                           bool sizeOnly, uint64_t offset, uint32_t length) {
    CliprdrClientContext *cliprdr = clipboard_begin(session);
    if (!cliprdr)
        return false;
    const CLIPRDR_FILE_CONTENTS_REQUEST request = {
        .common = { .msgType = CB_FILECONTENTS_REQUEST },
        .streamId = streamId,
        .listIndex = fileIndex,
        .dwFlags = sizeOnly ? FILECONTENTS_SIZE : FILECONTENTS_RANGE,
        .nPositionLow = sizeOnly ? 0 : (UINT32)(offset & 0xFFFFFFFF),
        .nPositionHigh = sizeOnly ? 0 : (UINT32)(offset >> 32),
        .cbRequested = sizeOnly ? 8 : length,
    };
    const UINT rc = cliprdr->ClientFileContentsRequest(cliprdr, &request);
    clipboard_end(session);
    return rc == CHANNEL_RC_OK;
}

bool wb_session_clipboard_file_respond(WBSession *session, uint32_t streamId, bool ok,
                                           const uint8_t *data, size_t size) {
    if (size > UINT32_MAX - 4 || (size > 0 && !data))
        ok = false;
    CliprdrClientContext *cliprdr = clipboard_begin(session);
    if (!cliprdr)
        return false;
    const CLIPRDR_FILE_CONTENTS_RESPONSE response = {
        .common = {
            .msgType = CB_FILECONTENTS_RESPONSE,
            .msgFlags = ok ? CB_RESPONSE_OK : CB_RESPONSE_FAIL,
            .dataLen = ok ? (UINT32)size + 4 : 4,
        },
        .streamId = streamId,
        .cbRequested = ok ? (UINT32)size : 0,
        .requestedData = ok ? data : NULL,
    };
    const UINT rc = cliprdr->ClientFileContentsResponse(cliprdr, &response);
    clipboard_end(session);
    return rc == CHANNEL_RC_OK;
}
