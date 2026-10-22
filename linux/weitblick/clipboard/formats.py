"""Clipboard formats on both sides and the flavors that map between them.

A flavor is one kind of content (text, HTML, RTF, image) with its local MIME types and its Windows
formats; its conversions are pure and may run on any thread. Files are handled separately
(weitblick.clipboard.files) because their contents travel as FileContents streams.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from ..rdp.session import RemoteClipboardFormat
from . import html as cf_html
from . import text as cf_text

CF_DIB = 8
CF_UNICODETEXT = 13
CF_DIBV5 = 17
#: First id of the range Windows uses for registered formats; ours are numbered from here.
FIRST_REGISTERED_ID = 0xC000


@dataclass(frozen=True)
class WindowsFormat:
    """A Windows clipboard format independent of the id a peer assigned to it: standard formats by
    id, registered formats by name (ids of registered formats differ per side)."""

    id: int | None = None
    name: str | None = None

    def matches(self, fmt: RemoteClipboardFormat) -> bool:
        """Registered names compare case-insensitively, like RegisterClipboardFormat."""
        if self.id is not None:
            return fmt.name is None and fmt.id == self.id
        return fmt.name is not None and self.name is not None and fmt.name.casefold() == self.name.casefold()

    def __str__(self) -> str:
        return self.name if self.name is not None else f"#{self.id}"


UNICODE_TEXT = WindowsFormat(id=CF_UNICODETEXT)
DIB = WindowsFormat(id=CF_DIB)
DIBV5 = WindowsFormat(id=CF_DIBV5)
HTML = WindowsFormat(name="HTML Format")
RTF = WindowsFormat(name="Rich Text Format")
PNG = WindowsFormat(name="PNG")
FILE_GROUP_DESCRIPTOR = WindowsFormat(name="FileGroupDescriptorW")

#: Local MIME types that carry copied files (Nautilus and most file managers).
FILE_MIMES = ("text/uri-list", "x-special/gnome-copied-files")


class Flavor:
    """One kind of clipboard content. `offered_mimes` are published for remote content (best first),
    `read_mimes` are read from local content (best first)."""

    name: str
    offered_mimes: tuple[str, ...]
    read_mimes: tuple[str, ...]
    windows_formats: tuple[WindowsFormat, ...]

    def offers(self, mimes: Collection[str], has_files: bool = False) -> bool:
        """Whether local content with these MIME types should be offered to the remote side."""
        return any(mime in mimes for mime in self.read_mimes)

    def to_windows(self, data: bytes, mime: str, fmt: WindowsFormat) -> bytes | None:
        raise NotImplementedError

    def to_local(self, data: bytes, fmt: WindowsFormat, mime: str) -> bytes | None:
        raise NotImplementedError


class TextFlavor(Flavor):
    """UTF-8 text <-> CF_UNICODETEXT. Local text is read with Gdk.Clipboard.read_text_async, which
    converts whatever charset the owner offers."""

    name = "text"
    offered_mimes = ("text/plain;charset=utf-8", "text/plain")
    read_mimes = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT")
    windows_formats = (UNICODE_TEXT,)

    def to_windows(self, data: bytes, mime: str, fmt: WindowsFormat) -> bytes | None:
        return cf_text.encode(data.decode("utf-8", "replace"))

    def to_local(self, data: bytes, fmt: WindowsFormat, mime: str) -> bytes | None:
        return cf_text.decode(data).encode("utf-8")


class HTMLFlavor(Flavor):
    """text/html <-> "HTML Format" (CF_HTML with its offset header)."""

    name = "html"
    offered_mimes = ("text/html",)
    read_mimes = ("text/html",)
    windows_formats = (HTML,)

    def to_windows(self, data: bytes, mime: str, fmt: WindowsFormat) -> bytes | None:
        return cf_html.encode(decode_local_html(data))

    def to_local(self, data: bytes, fmt: WindowsFormat, mime: str) -> bytes | None:
        decoded = cf_html.decode(data)
        return None if decoded is None else decoded.encode("utf-8")


class RTFFlavor(Flavor):
    """text/rtf <-> "Rich Text Format". RTF is 7-bit text on both sides; Windows adds a NUL."""

    name = "rtf"
    offered_mimes = ("text/rtf", "application/rtf")
    read_mimes = ("text/rtf", "application/rtf", "text/richtext")
    windows_formats = (RTF,)

    def to_windows(self, data: bytes, mime: str, fmt: WindowsFormat) -> bytes | None:
        return data + b"\0"

    def to_local(self, data: bytes, fmt: WindowsFormat, mime: str) -> bytes | None:
        nul = data.find(b"\0")
        return data if nul < 0 else data[:nul]


class ImageFlavor(Flavor):
    """image/png (and other local image types when reading) <-> "PNG", CF_DIBV5, CF_DIB."""

    name = "image"
    offered_mimes = ("image/png",)
    read_mimes = ("image/png", "image/bmp", "image/x-bmp", "image/tiff", "image/jpeg", "image/gif",
                  "image/webp")
    windows_formats = (PNG, DIBV5, DIB)

    def offers(self, mimes: Collection[str], has_files: bool = False) -> bool:
        # File managers may put icons next to copied files; those are not the user's image.
        return not has_files and any(mime in mimes for mime in self.read_mimes)

    def to_windows(self, data: bytes, mime: str, fmt: WindowsFormat) -> bytes | None:
        from . import dib, image
        if fmt == PNG and image.is_png(data):
            return data
        decoded = image.decode_image(data)
        if decoded is None:
            return None
        if fmt == PNG:
            return image.encode_png(decoded)
        if fmt == DIBV5:
            return dib.encode_dibv5(decoded)
        if fmt == DIB:
            return dib.encode_dib(decoded)
        return None

    def to_local(self, data: bytes, fmt: WindowsFormat, mime: str) -> bytes | None:
        from . import dib, image
        if fmt == PNG and image.is_png(data):
            return data
        decoded = image.decode_image(data) if fmt == PNG else dib.decode_dib(data)
        return None if decoded is None else image.encode_png(decoded)


FLAVORS: tuple[Flavor, ...] = (TextFlavor(), HTMLFlavor(), RTFFlavor(), ImageFlavor())


def decode_local_html(data: bytes) -> str:
    """Local text/html bytes: UTF-8 usually, UTF-16 with a BOM from some (older Mozilla) apps."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    return data.decode("utf-8", "replace").lstrip("﻿")


@dataclass(frozen=True)
class Offer:
    """A remote format chosen for a flavor: `format_id` is the server's id for `fmt`."""

    flavor: Flavor
    fmt: WindowsFormat
    format_id: int


def offers_for(formats: list[RemoteClipboardFormat], flavors: tuple[Flavor, ...] = FLAVORS) -> dict[str, Offer]:
    """The flavors to publish for a server format list, by flavor name. Text wins over images: Office
    puts a rendered picture next to copied cells or text, which local apps would otherwise paste."""
    has_text = any(UNICODE_TEXT.matches(fmt) for fmt in formats)
    offers: dict[str, Offer] = {}
    for flavor in flavors:
        if has_text and isinstance(flavor, ImageFlavor):
            continue
        for windows_format in flavor.windows_formats:
            match = next((fmt for fmt in formats if windows_format.matches(fmt)), None)
            if match is not None:
                offers[flavor.name] = Offer(flavor, windows_format, match.id)
                break
    return offers
