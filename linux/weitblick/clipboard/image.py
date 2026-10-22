"""Image files (PNG, JPEG, TIFF, BMP, …) <-> BGRAImage through GDK's texture loaders.

GdkTexture objects are immutable, and memory textures need no display, so this runs on worker
threads too.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib  # noqa: E402

from .dib import BGRAImage  # noqa: E402

log = logging.getLogger(__name__)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def is_png(data: bytes) -> bool:
    return data.startswith(PNG_SIGNATURE)


def decode_image(data: bytes) -> BGRAImage | None:
    try:
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
    except GLib.Error as error:
        log.info("image not decodable: %s", error.message)
        return None
    return from_texture(texture)


def from_texture(texture: Gdk.Texture) -> BGRAImage | None:
    width, height = texture.get_width(), texture.get_height()
    if width <= 0 or height <= 0:
        return None
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.B8G8R8A8)  # straight alpha
    pixels, stride = downloader.download_bytes()
    raw = pixels.get_data()
    if stride != width * 4:
        raw = b"".join(raw[row * stride:row * stride + width * 4] for row in range(height))
    return BGRAImage(width, height, bytes(raw))


def to_texture(image: BGRAImage) -> Gdk.Texture:
    return Gdk.MemoryTexture.new(image.width, image.height, Gdk.MemoryFormat.B8G8R8A8,
                                 GLib.Bytes.new(image.data), image.width * 4)


def encode_png(image: BGRAImage) -> bytes:
    return to_texture(image).save_to_png_bytes().get_data()
