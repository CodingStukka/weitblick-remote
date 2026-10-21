// Framebuffer (GDI): dirty tracking, desktop resize and the locked view for the app.
#include "wb_internal.h"

#include <stdlib.h>
#include <string.h>

#include <freerdp/gdi/gdi.h>
#include <winpr/wlog.h>

// MARK: - Frames

static void signal_frame(WBSession *session) {
    if (atomic_exchange(&session->framePending, true))
        return;
    if (session->callbacks.frameReady)
        session->callbacks.frameReady(session->callbacks.userData);
}

static void add_dirty_rect(WBSession *session, rdpGdi *gdi, INT32 x, INT32 y, INT32 w, INT32 h) {
    const INT32 left = x < 0 ? 0 : x;
    const INT32 top = y < 0 ? 0 : y;
    const INT32 right = (x + w) > gdi->width ? gdi->width : (x + w);
    const INT32 bottom = (y + h) > gdi->height ? gdi->height : (y + h);
    if (right <= left || bottom <= top)
        return;
    const RECTANGLE_16 rect = { (UINT16)left, (UINT16)top, (UINT16)right, (UINT16)bottom };
    if (!region16_union_rect(&session->dirty, &session->dirty, &rect))
        WLog_WARN(WB_TAG, "region16_union_rect failed");
}

// Called by FreeRDP with the update lock held, after a batch of drawing.
static BOOL on_end_paint(rdpContext *context) {
    WBSession *session = wb_session_from_context(context);
    rdpGdi *gdi = context->gdi;
    if (!gdi || !gdi->primary || !gdi->primary->hdc || !gdi->primary->hdc->hwnd)
        return TRUE;

    HGDI_WND hwnd = gdi->primary->hdc->hwnd;
    if (hwnd->invalid->null)
        return TRUE;
    for (INT32 i = 0; i < hwnd->ninvalid; i++) {
        const GDI_RGN *r = &hwnd->cinvalid[i];
        add_dirty_rect(session, gdi, r->x, r->y, r->w, r->h);
    }
    hwnd->ninvalid = 0;
    hwnd->invalid->null = TRUE;
    signal_frame(session);
    return TRUE;
}

static BOOL on_desktop_resize(rdpContext *context) {
    WBSession *session = wb_session_from_context(context);
    const uint32_t width = freerdp_settings_get_uint32(context->settings, FreeRDP_DesktopWidth);
    const uint32_t height = freerdp_settings_get_uint32(context->settings, FreeRDP_DesktopHeight);

    rdp_update_lock(context->update);
    const BOOL ok = gdi_resize(context->gdi, width, height);
    region16_clear(&session->dirty);
    if (ok)
        add_dirty_rect(session, context->gdi, 0, 0, (INT32)width, (INT32)height);
    rdp_update_unlock(context->update);

    wb_resolution_answered(session, width, height);
    if (session->callbacks.desktopResized)
        session->callbacks.desktopResized(session->callbacks.userData, width, height);
    signal_frame(session);
    return ok;
}

bool wb_session_framebuffer_acquire(WBSession *session, WBFramebuffer *out) {
    if (!session || !out || !session->context)
        return false;
    rdpContext *context = session->context;
    rdp_update_lock(context->update);

    rdpGdi *gdi = context->gdi;
    if (!gdi || !gdi->primary_buffer) {
        rdp_update_unlock(context->update);
        return false;
    }
    UINT32 count = 0;
    const RECTANGLE_16 *rects = region16_rects(&session->dirty, &count);
    if (count > session->rectScratchCapacity) {
        WBRect *grown = realloc(session->rectScratch, count * sizeof(WBRect));
        if (!grown) {
            rdp_update_unlock(context->update);
            return false; // the region stays dirty for the next attempt
        }
        session->rectScratch = grown;
        session->rectScratchCapacity = count;
    }
    for (UINT32 i = 0; i < count; i++) {
        session->rectScratch[i] = (WBRect){
            rects[i].left, rects[i].top, rects[i].right - rects[i].left, rects[i].bottom - rects[i].top
        };
    }
    region16_clear(&session->dirty);
    // Cleared while still locked, so any paint after release signals a new frame.
    atomic_store(&session->framePending, false);

    *out = (WBFramebuffer){
        .pixels = gdi->primary_buffer,
        .width = (uint32_t)gdi->width,
        .height = (uint32_t)gdi->height,
        .stride = gdi->stride,
        .dirtyRects = session->rectScratch,
        .dirtyRectCount = count,
    };
    return true;
}

bool wb_session_framebuffer_peek(WBSession *session, WBFramebuffer *out) {
    if (!session || !out || !session->context)
        return false;
    rdpContext *context = session->context;
    rdp_update_lock(context->update);

    rdpGdi *gdi = context->gdi;
    if (!gdi || !gdi->primary_buffer) {
        rdp_update_unlock(context->update);
        return false;
    }
    // Dirty rects and the frame signal belong to the presenter (acquire); a peek leaves them alone.
    *out = (WBFramebuffer){
        .pixels = gdi->primary_buffer,
        .width = (uint32_t)gdi->width,
        .height = (uint32_t)gdi->height,
        .stride = gdi->stride,
        .dirtyRects = NULL,
        .dirtyRectCount = 0,
    };
    return true;
}

void wb_session_framebuffer_release(WBSession *session) {
    if (session && session->context)
        rdp_update_unlock(session->context->update);
}

// MARK: - Install

bool wb_display_install(WBSession *session) {
    rdpContext *context = session->context;
    if (!gdi_init(context->instance, PIXEL_FORMAT_BGRA32))
        return false;

    wb_pointer_register(context);

    context->update->EndPaint = on_end_paint;
    context->update->DesktopResize = on_desktop_resize;
    return true;
}

void wb_display_uninstall(WBSession *session) {
    rdpContext *context = session->context;
    // The app may be reading the framebuffer right now; gdi_free must wait for it.
    rdp_update_lock(context->update);
    gdi_free(context->instance);
    region16_clear(&session->dirty);
    rdp_update_unlock(context->update);
}
