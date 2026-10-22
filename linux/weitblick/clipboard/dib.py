"""CF_DIB / CF_DIBV5: a BITMAPINFOHEADER (40 bytes) or BITMAPV5HEADER (124 bytes), optional color
masks and palette, then bottom-up pixel rows padded to 4 bytes.

Pure Python on byte slices (no per-pixel loops for 24/32 bpp); other depths go through a BMP file
and GDK's image loaders (weitblick.clipboard.image).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

BI_RGB = 0
BI_BITFIELDS = 3
BI_ALPHABITFIELDS = 6
INFO_HEADER_SIZE = 40
V5_HEADER_SIZE = 124
_MAX_SIDE = 32768


@dataclass(frozen=True)
class BGRAImage:
    """Pixels as B G R A bytes, straight (not premultiplied) alpha, top-down, rows packed (width * 4)."""

    width: int
    height: int
    data: bytes

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or len(self.data) != self.width * self.height * 4:
            raise ValueError("BGRAImage: size and data do not match")

    @property
    def is_opaque(self) -> bool:
        alpha = self.data[3::4]
        return alpha.count(255) == len(alpha)

    def rgba(self) -> bytes:
        """The same pixels as R G B A."""
        out = bytearray(self.data)
        out[0::4] = self.data[2::4]
        out[2::4] = self.data[0::4]
        return bytes(out)


# MARK: Encode

def encode_dib(image: BGRAImage) -> bytes:
    """CF_DIB for apps that know no transparency: 24 bpp, transparent areas flattened onto white."""
    width, height = image.width, image.height
    bgra = _flatten_onto_white(image)
    bgr = bytearray(width * height * 3)
    for channel in range(3):  # memory order B G R X: dropping the fourth byte leaves B G R
        bgr[channel::3] = bgra[channel::4]
    stride = (width * 3 + 3) & ~3
    padding = b"\0" * (stride - width * 3)
    row_bytes = width * 3
    rows = [bytes(bgr[row * row_bytes:(row + 1) * row_bytes]) + padding for row in range(height - 1, -1, -1)]
    return _info_header(width, height, 24, BI_RGB, stride * height) + b"".join(rows)


def encode_dibv5(image: BGRAImage) -> bytes:
    """CF_DIBV5 with straight alpha: 32 bpp BI_BITFIELDS (BGRA), sRGB."""
    width, height = image.width, image.height
    stride = width * 4
    header = _info_header(width, height, 32, BI_BITFIELDS, stride * height, header_size=V5_HEADER_SIZE)
    header += struct.pack("<IIII", 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)  # R, G, B, A masks
    header += struct.pack("<I", 0x73524742)  # LCS_sRGB 'sRGB'
    header += bytes(36)  # CIEXYZTRIPLE endpoints, unused for sRGB
    header += bytes(12)  # gamma red/green/blue
    header += struct.pack("<I", 4)  # LCS_GM_IMAGES
    header += bytes(12)  # profile data, profile size, reserved
    rows = [image.data[row * stride:(row + 1) * stride] for row in range(height - 1, -1, -1)]
    return header + b"".join(rows)


def _info_header(width: int, height: int, bit_count: int, compression: int, image_size: int,
                 header_size: int = INFO_HEADER_SIZE) -> bytes:
    # Positive height: bottom-up. 2835 pixels per meter = 72 dpi.
    return struct.pack("<IiiHHIIiiII", header_size, width, height, 1, bit_count, compression, image_size,
                       2835, 2835, 0, 0)


def _flatten_onto_white(image: BGRAImage) -> bytes:
    """BGRA with every pixel composited onto white (alpha 255)."""
    if image.is_opaque:
        return image.data
    try:
        return _flatten_with_pixbuf(image)
    except (ImportError, ValueError):
        pass
    out = bytearray(image.data)
    for index in range(3, len(out), 4):
        alpha = out[index]
        if alpha != 255:
            for channel in range(index - 3, index):
                out[channel] = (out[channel] * alpha + 255 * (255 - alpha) + 127) // 255
            out[index] = 255
    return bytes(out)


def _flatten_with_pixbuf(image: BGRAImage) -> bytes:
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib

    width, height = image.width, image.height
    source = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(image.rgba()), GdkPixbuf.Colorspace.RGB, True, 8,
                                             width, height, width * 4)
    flat = source.composite_color_simple(width, height, GdkPixbuf.InterpType.NEAREST, 255, 8, 0xFFFFFF, 0xFFFFFF)
    if flat is None or flat.get_n_channels() != 4:
        raise ValueError("compositing failed")
    stride = flat.get_rowstride()
    pixels = flat.get_pixels()
    rgba = b"".join(pixels[row * stride:row * stride + width * 4] for row in range(height))
    out = bytearray(rgba)
    out[0::4] = rgba[2::4]
    out[2::4] = rgba[0::4]
    out[3::4] = b"\xff" * (width * height)
    return bytes(out)


# MARK: Decode

def decode_dib(data: bytes) -> BGRAImage | None:
    """24 and 32 bpp are decoded here (Windows leaves the fourth byte of a 32 bpp CF_DIB undefined,
    mostly 0, which a BMP loader would read as fully transparent); everything else goes to GDK."""
    if len(data) < INFO_HEADER_SIZE:
        return None
    header_size, width, raw_height, _planes, bit_count, compression = struct.unpack_from("<IiiHHI", data, 0)
    colors_used = struct.unpack_from("<I", data, 32)[0]
    if not (INFO_HEADER_SIZE <= header_size <= len(data) and 0 < width <= _MAX_SIDE
            and raw_height != 0 and abs(raw_height) <= _MAX_SIDE):
        return None
    height = abs(raw_height)

    mask_bytes = 0
    if header_size == INFO_HEADER_SIZE:
        if compression == BI_BITFIELDS:
            mask_bytes = 12
        elif compression == BI_ALPHABITFIELDS:
            mask_bytes = 16
    elif compression == BI_BITFIELDS and len(data) >= header_size + 12 \
            and data[header_size:header_size + 12] == data[40:52]:
        # Windows' synthesized CF_DIBV5 repeats the masks after the V5 header, like a color table.
        mask_bytes = 12
    palette_entries = (1 << bit_count if colors_used == 0 else colors_used) if bit_count <= 8 else colors_used
    pixel_offset = header_size + mask_bytes + palette_entries * 4

    if bit_count not in (24, 32):
        return _decode_as_bmp_file(data, pixel_offset)
    alpha_mask = 0
    if compression == BI_RGB:
        alpha_mask = 0xFF000000 if bit_count == 32 else 0
    elif compression in (BI_BITFIELDS, BI_ALPHABITFIELDS):
        # Masks follow a 40-byte header or sit at the same place inside V2…V5 headers.
        if bit_count != 32 or len(data) < 52 or struct.unpack_from("<III", data, 40) != (0x00FF0000, 0x0000FF00, 0x000000FF):
            return _decode_as_bmp_file(data, pixel_offset)
        if (compression == BI_ALPHABITFIELDS or header_size >= 56) and len(data) >= 56:
            alpha_mask = struct.unpack_from("<I", data, 52)[0]
        if alpha_mask not in (0, 0xFF000000):
            return _decode_as_bmp_file(data, pixel_offset)
    else:
        return _decode_as_bmp_file(data, pixel_offset)

    row_stride = (width * bit_count + 31) // 32 * 4
    image_size = row_stride * height
    # Some writers put extra masks or a profile in front of the bits; trust the total size then.
    offset = pixel_offset if pixel_offset + image_size <= len(data) else len(data) - image_size
    if offset < header_size:
        return None
    row_bytes = width * bit_count // 8
    rows = []
    for row in range(height):
        source = offset + ((height - 1 - row) if raw_height > 0 else row) * row_stride
        rows.append(data[source:source + row_bytes])
    packed = b"".join(rows)
    if bit_count == 32:
        alpha = packed[3::4]
        # An all-zero fourth byte means "no alpha", not "invisible".
        has_alpha = alpha_mask != 0 and alpha.count(0) != len(alpha)
        if has_alpha:
            return BGRAImage(width, height, packed)
        out = bytearray(packed)
        out[3::4] = b"\xff" * (width * height)
        return BGRAImage(width, height, bytes(out))
    out = bytearray(width * height * 4)
    for channel in range(3):  # B G R -> B G R 255
        out[channel::4] = packed[channel::3]
    out[3::4] = b"\xff" * (width * height)
    return BGRAImage(width, height, bytes(out))


def _decode_as_bmp_file(data: bytes, pixel_offset: int) -> BGRAImage | None:
    """Palette, 16 bpp and RLE bitmaps: add a BITMAPFILEHEADER and let GDK's loaders read the BMP."""
    bmp = b"BM" + struct.pack("<III", 14 + len(data), 0, 14 + pixel_offset) + data
    try:
        from .image import decode_image
    except ImportError:
        return None
    return decode_image(bmp)
