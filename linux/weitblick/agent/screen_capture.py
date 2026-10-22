"""Screenshots for agents (port of ScreenCapture.swift): pixels copied from a session's framebuffer
without taking the view's dirty rectangles (the session may be on screen at the same time), scaled
to screenshot size and encoded as PNG, or JPEG when the PNG would be large. The encoding helpers
block: run them off the main loop (loop.run_in_thread)."""

from __future__ import annotations

from dataclasses import dataclass

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

from .screen_mapping import DesktopRect  # noqa: E402

#: Encoded images above this size go out as JPEG instead of PNG.
PNG_LIMIT = 1_000_000
JPEG_QUALITY = 85


@dataclass(frozen=True)
class DesktopPixels:
    """BGRA pixels (4 bytes each, rows packed) and where they sit on the desktop."""

    bgra: bytes
    rect: DesktopRect


def crop(frame, rect: DesktopRect | None = None) -> DesktopPixels | None:
    """`rect` of a frame (all of it when None), clipped to the framebuffer; None if nothing is left.
    `frame` is a weitblick.rdp.session.Frame (data, width, height, stride)."""
    wanted = rect or DesktopRect(0, 0, frame.width, frame.height)
    min_x, min_y = max(0, wanted.x), max(0, wanted.y)
    max_x = min(frame.width, wanted.x + wanted.width)
    max_y = min(frame.height, wanted.y + wanted.height)
    if max_x <= min_x or max_y <= min_y:
        return None
    clipped = DesktopRect(min_x, min_y, max_x - min_x, max_y - min_y)
    row_bytes = clipped.width * 4
    if clipped.x == 0 and row_bytes == frame.stride:
        start = clipped.y * frame.stride
        return DesktopPixels(bytes(frame.data[start:start + row_bytes * clipped.height]), clipped)
    view = memoryview(frame.data)
    rows = [view[(clipped.y + row) * frame.stride + clipped.x * 4:][:row_bytes] for row in range(clipped.height)]
    return DesktopPixels(b"".join(rows), clipped)


def looks_blank(frame) -> bool:
    """Whether the picture is one flat color (a session still signing in shows white, black or blue
    for a while): a 64×40 grid of samples with fewer than 6 distinct colors (6 bits each)."""
    if frame is None or frame.width <= 0 or frame.height <= 0:
        return True
    data, stride = frame.data, frame.stride
    colors: set[bytes] = set()
    for row in range(40):
        y = (frame.height - 1) * row // 39
        for column in range(64):
            x = (frame.width - 1) * column // 63
            offset = y * stride + x * 4
            colors.add(bytes(value & 0xFC for value in data[offset:offset + 3]))
            if len(colors) >= 6:
                return False
    return True


def bgra_to_rgb(bgra: bytes, width: int, height: int) -> bytes:
    """Packed BGRA (alpha ignored) as packed RGB, for GdkPixbuf. Slices run at C speed."""
    count = width * height
    rgb = bytearray(count * 3)
    rgb[0::3] = bgra[2::4][:count]
    rgb[1::3] = bgra[1::4][:count]
    rgb[2::3] = bgra[0::4][:count]
    return bytes(rgb)


def pixbuf(pixels: DesktopPixels) -> GdkPixbuf.Pixbuf:
    width, height = pixels.rect.width, pixels.rect.height
    return GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(bgra_to_rgb(pixels.bgra, width, height)),
                                           GdkPixbuf.Colorspace.RGB, False, 8, width, height, width * 3)


def resized(image: GdkPixbuf.Pixbuf, width: int, height: int) -> GdkPixbuf.Pixbuf:
    """Smooth downscaling for screenshots, plain upscaling for text recognition."""
    if image.get_width() == width and image.get_height() == height:
        return image
    smaller = width < image.get_width()
    return image.scale_simple(width, height, GdkPixbuf.InterpType.HYPER if smaller else GdkPixbuf.InterpType.BILINEAR)


def encode(image: GdkPixbuf.Pixbuf, png_limit: int = PNG_LIMIT) -> tuple[bytes, str]:
    """PNG, or JPEG when the PNG would be larger than `png_limit` (photos, busy wallpapers)."""
    ok, png = image.save_to_bufferv("png", [], [])
    if ok and len(png) <= png_limit:
        return bytes(png), "image/png"
    ok, jpeg = image.save_to_bufferv("jpeg", ["quality"], [str(JPEG_QUALITY)])
    if not ok:
        raise RuntimeError("the screenshot couldn't be encoded")
    return bytes(jpeg), "image/jpeg"


def screenshot(pixels: DesktopPixels, width: int, height: int) -> tuple[bytes, str]:
    """The pixels scaled to `width`×`height` and encoded (blocks)."""
    return encode(resized(pixbuf(pixels), width, height))


def ocr_png(pixels: DesktopPixels, factor: int) -> bytes:
    """The pixels as PNG, `factor` times larger (small UI text reads better at twice the size)."""
    image = pixbuf(pixels)
    if factor != 1:
        image = resized(image, image.get_width() * factor, image.get_height() * factor)
    ok, png = image.save_to_bufferv("png", [], [])
    if not ok:
        raise RuntimeError("the image for text recognition couldn't be encoded")
    return bytes(png)
