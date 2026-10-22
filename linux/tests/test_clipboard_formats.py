"""Clipboard format conversions (port of ClipboardFormatTests and ClipboardOfferTests)."""

import re
import struct
import unittest

from weitblick.clipboard import dib, formats, html, text
from weitblick.clipboard.dib import BGRAImage
from weitblick.rdp.session import RemoteClipboardFormat


def make_image(width, height, pixel):
    """BGRAImage from pixel(x, y) -> (r, g, b, a)."""
    data = bytearray()
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixel(x, y)
            data += bytes([b, g, r, a])
    return BGRAImage(width, height, bytes(data))


def info_header(width, height, bit_count):
    return struct.pack("<IiiHHIIiiII", 40, width, height, 1, bit_count, 0, 0, 2835, 2835, 0, 0)


def offsets(data):
    header = data[:160].decode("utf-8", "replace")
    values = {}
    for key in ("StartHTML", "EndHTML", "StartFragment", "EndFragment"):
        match = re.search(key + r":(-?\d+)", header)
        values[key] = int(match.group(1))
    return values


class WindowsTextTests(unittest.TestCase):
    def test_text_gets_crlf_and_a_terminator(self):
        self.assertEqual(text.encode("a\nb\r\nc\rd😀"), "a\r\nb\r\nc\r\nd😀".encode("utf-16-le") + b"\0\0")

    def test_text_stops_at_the_terminator_and_gets_lf(self):
        data = "Grüße\r\nWelt 😀".encode("utf-16-le") + b"\0\0A\0"  # garbage after NUL, as in GlobalAlloc padding
        self.assertEqual(text.decode(data), "Grüße\nWelt 😀")

    def test_unpaired_surrogates_and_odd_lengths_are_lenient(self):
        self.assertEqual(text.decode(b"\x00\xd8A\x00\x42"), "�A")


class ClipboardHTMLTests(unittest.TestCase):
    def test_fragment_offsets_point_at_the_fragment(self):
        fragment = "<b>Grüße</b> &amp; 😀"
        data = html.encode(fragment)
        o = offsets(data)
        self.assertEqual(data[o["StartFragment"]:o["EndFragment"]].decode("utf-8"), fragment)
        self.assertTrue(data[o["StartHTML"]:].startswith(b"<html><body><!--StartFragment-->"))
        self.assertEqual(data[:o["EndHTML"]][-7:], b"</html>")
        self.assertEqual(data[-1], 0)

    def test_document_keeps_its_head_and_uses_the_body_as_fragment(self):
        document = "<html><head><style>p{color:red}</style></head><BODY class=x><p>Hi</p></BODY></html>"
        data = html.encode(document)
        o = offsets(data)
        self.assertEqual(data[o["StartFragment"]:o["EndFragment"]], b"<p>Hi</p>")
        self.assertIn(b"<style>", data[o["StartHTML"]:o["EndHTML"]])

    def test_html_from_windows_gets_a_utf8_charset_and_round_trips(self):
        decoded = html.decode(html.encode("<i>Größe</i>"))
        self.assertTrue(decoded.startswith('<meta charset="utf-8"><html><body><!--StartFragment--><i>Größe</i>'),
                        decoded)

        # Without StartHTML/EndHTML (-1) the fragment offsets are used.
        def header(start, end):
            return f"Version:0.9\r\nStartHTML:-1\r\nEndHTML:-1\r\nStartFragment:{start:010d}\r\nEndFragment:{end:010d}\r\n"
        before = "<html><body><!--StartFragment-->"
        start = len(header(0, 0).encode()) + len(before.encode())
        blob = header(start, start + len("Grüße".encode())) + before + "Grüße<!--EndFragment--></body></html>"
        self.assertEqual(html.decode(blob.encode("utf-8")), '<meta charset="utf-8">Grüße')

    def test_meta_goes_into_an_existing_head(self):
        self.assertEqual(html.decode(b"Version:0.9\r\n<html><head></head></html>"),
                         '<html><head><meta charset="utf-8"></head></html>')

    def test_local_html_in_utf16_is_decoded(self):
        self.assertEqual(formats.decode_local_html("<b>ä</b>".encode("utf-16")), "<b>ä</b>")


class DeviceIndependentBitmapTests(unittest.TestCase):
    def test_dib_is_bottom_up_24_bit_over_white(self):
        image = make_image(3, 2, lambda x, y: (0, 0, 0, 0) if x == 0 else (x * 50, y * 90, 7, 255))
        data = dib.encode_dib(image)
        self.assertEqual(data[0], 40)  # BITMAPINFOHEADER
        self.assertEqual(data[14], 24)  # bpp
        self.assertEqual(len(data), 40 + 2 * 12)  # rows of 9 bytes padded to 12
        # First stored row is the bottom one (y = 1); transparent pixel 0 became white; B G R order.
        self.assertEqual(list(data[40:49]), [255, 255, 255, 7, 90, 50, 7, 90, 100])

    def test_half_transparent_pixels_are_blended_onto_white(self):
        image = make_image(1, 1, lambda x, y: (0, 0, 255, 128))
        data = dib.encode_dib(image)
        self.assertEqual(data[40], 255)  # blue stays (B G R order)
        self.assertTrue(125 <= data[41] <= 128)  # green: half white
        self.assertTrue(125 <= data[42] <= 128)  # red: half white

    def test_dibv5_round_trip_keeps_pixels_and_alpha(self):
        image = make_image(5, 4, lambda x, y: (x * 40, y * 60, x * y, 128 if x == 4 else 255))
        data = dib.encode_dibv5(image)
        self.assertEqual(data[0], 124)
        self.assertEqual(len(data), 124 + 5 * 4 * 4)
        decoded = dib.decode_dib(data)
        self.assertEqual(decoded, image)
        rgba = decoded.rgba()
        self.assertEqual(list(rgba[(1 * 5 + 2) * 4:(1 * 5 + 2) * 4 + 4]), [80, 60, 2, 255])
        self.assertEqual(rgba[(3 * 5 + 4) * 4 + 3], 128)

    def test_windows_dibv5_with_repeated_masks_decodes_at_the_right_offset(self):
        # Windows' synthesized CF_DIBV5 has the three BI_BITFIELDS masks again after the header.
        image = make_image(4, 3, lambda x, y: (x * 60, y * 80, 9, 255))
        data = dib.encode_dibv5(image)
        data = data[:124] + data[40:52] + data[124:]
        self.assertEqual(dib.decode_dib(data), image)

    def test_thirty_two_bit_dib_without_alpha_is_opaque(self):
        # Windows leaves the fourth byte of a 32 bpp BI_RGB CF_DIB at 0; that must not mean invisible.
        data = info_header(2, -1, 32) + bytes([10, 20, 30, 0, 40, 50, 60, 0])  # top-down: B G R X
        self.assertEqual(dib.decode_dib(data).rgba(), bytes([30, 20, 10, 255, 60, 50, 40, 255]))

    def test_twenty_four_bit_round_trip(self):
        image = make_image(3, 2, lambda x, y: (x * 70, y * 100, 200, 255))
        self.assertEqual(dib.decode_dib(dib.encode_dib(image)), image)

    def test_rejects_garbage(self):
        self.assertIsNone(dib.decode_dib(b"\x28\0\0\0"))
        self.assertIsNone(dib.decode_dib(info_header(0, 5, 24)))


class ImageCodecTests(unittest.TestCase):
    def setUp(self):
        try:
            from weitblick.clipboard import image
        except (ImportError, ValueError) as error:
            self.skipTest(f"GDK not available: {error}")
        self.image = image

    def test_png_round_trip(self):
        source = make_image(3, 2, lambda x, y: (x * 80, y * 100, 5, 255 if x else 64))
        png = self.image.encode_png(source)
        self.assertTrue(self.image.is_png(png))
        self.assertEqual(self.image.decode_image(png), source)

    def test_image_flavor_converts_dib_to_png_and_back(self):
        flavor = formats.ImageFlavor()
        source = make_image(2, 2, lambda x, y: (x * 200, y * 200, 50, 255))
        png = flavor.to_local(dib.encode_dibv5(source), formats.DIBV5, "image/png")
        self.assertEqual(self.image.decode_image(png), source)
        self.assertEqual(dib.decode_dib(flavor.to_windows(png, "image/png", formats.DIB)), source)
        self.assertEqual(flavor.to_windows(png, "image/png", formats.PNG), png)

    def test_palette_bitmaps_go_through_the_loader(self):
        # 1 bpp, 2x1, palette black/white, pixels 1 0 -> white, black.
        data = struct.pack("<IiiHHIIiiII", 40, 2, 1, 1, 1, 0, 4, 2835, 2835, 2, 0)
        data += bytes([0, 0, 0, 0, 255, 255, 255, 0]) + bytes([0b10000000, 0, 0, 0])
        decoded = dib.decode_dib(data)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.rgba(), bytes([255, 255, 255, 255, 0, 0, 0, 255]))


class ClipboardOfferTests(unittest.TestCase):
    def test_text_wins_over_images_and_png_over_dib(self):
        office = [RemoteClipboardFormat(13), RemoteClipboardFormat(8), RemoteClipboardFormat(0xC0A1, "html format")]
        offers = formats.offers_for(office)
        self.assertEqual(set(offers), {"text", "html"})
        self.assertEqual(offers["html"].format_id, 0xC0A1)

        picture = [RemoteClipboardFormat(8), RemoteClipboardFormat(17), RemoteClipboardFormat(0xC123, "PNG")]
        offers = formats.offers_for(picture)
        self.assertEqual(set(offers), {"image"})
        self.assertEqual(offers["image"].fmt, formats.PNG)
        self.assertEqual(offers["image"].format_id, 0xC123)

    def test_registered_names_match_case_insensitively_standard_ids_only_without_name(self):
        self.assertTrue(formats.HTML.matches(RemoteClipboardFormat(0xC001, "HTML FORMAT")))
        self.assertFalse(formats.UNICODE_TEXT.matches(RemoteClipboardFormat(13, "something")))
        self.assertTrue(formats.UNICODE_TEXT.matches(RemoteClipboardFormat(13)))

    def test_file_manager_icons_are_not_offered_as_images(self):
        flavor = formats.ImageFlavor()
        self.assertTrue(flavor.offers({"image/png"}))
        self.assertFalse(flavor.offers({"image/png", "text/uri-list"}, has_files=True))

    def test_rtf_gets_and_loses_its_nul(self):
        flavor = formats.RTFFlavor()
        self.assertEqual(flavor.to_windows(b"{\\rtf1 x}", "text/rtf", formats.RTF), b"{\\rtf1 x}\0")
        self.assertEqual(flavor.to_local(b"{\\rtf1 x}\0junk", formats.RTF, "text/rtf"), b"{\\rtf1 x}")


if __name__ == "__main__":
    unittest.main()
