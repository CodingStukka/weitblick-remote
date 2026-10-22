import os
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from weitblick.agent import frame_diff, text_matching
from weitblick.agent.arguments import ToolArguments
from weitblick.agent.audit import AuditEntry, AuditLog
from weitblick.agent.errors import ToolArgumentError
from weitblick.agent.frame_diff import SettleTracker
from weitblick.agent.screen_mapping import DesktopRect, PixelPoint, PixelRect, ScreenMapping


class ScreenMappingTests(unittest.TestCase):
    def test_agent_desktops_are_one_to_one(self):
        mapping = ScreenMapping(1280, 800)
        self.assertEqual(mapping.scale, 1)
        self.assertEqual((mapping.screenshot_width, mapping.screenshot_height), (1280, 800))
        self.assertEqual(mapping.desktop_point(PixelPoint(640, 400)), (640, 400))
        self.assertEqual(mapping.desktop_point(PixelPoint(1279.4, 799)), (1279, 799))
        self.assertIn("scale 1", mapping.summary)

    def test_large_desktops_are_scaled_down_and_mapped_back(self):
        retina = ScreenMapping(2560, 1600)
        self.assertEqual(retina.scale, 0.5)
        self.assertEqual((retina.screenshot_width, retina.screenshot_height), (1280, 800))
        self.assertEqual(retina.desktop_point(PixelPoint(100, 50)), (200, 100))
        self.assertEqual(retina.desktop_rect(PixelRect(10, 10, 100, 50)), DesktopRect(20, 20, 200, 100))
        self.assertEqual(retina.screenshot_rect(PixelRect(200, 100, 40, 20)), PixelRect(100, 50, 20, 10))

        full_hd = ScreenMapping(1920, 1080)
        self.assertEqual((full_hd.screenshot_width, full_hd.screenshot_height), (1280, 720))
        self.assertEqual(full_hd.desktop_point(PixelPoint(1279, 719)), (1918, 1078))
        self.assertIn("desktop 1920×1080", full_hd.summary)
        self.assertIn("scale 0.6667", full_hd.summary)

        portrait = ScreenMapping(1200, 1920)
        self.assertEqual((portrait.screenshot_width, portrait.screenshot_height), (800, 1280))

    def test_points_outside_the_screen_are_refused(self):
        mapping = ScreenMapping(1280, 800)
        with self.assertRaises(ToolArgumentError):
            mapping.desktop_point(PixelPoint(-1, 10))
        with self.assertRaises(ToolArgumentError):
            mapping.desktop_point(PixelPoint(10, 801))
        with self.assertRaises(ToolArgumentError):
            mapping.desktop_rect(PixelRect(1300, 0, 10, 10))
        # Regions reaching over the edge are clipped.
        self.assertEqual(mapping.desktop_rect(PixelRect(1200, 700, 500, 500)), DesktopRect(1200, 700, 80, 100))


def frame(width, height, fill=0xFF000000):
    return [fill] * (width * height)


def pack(pixels):
    return struct.pack(f"<{len(pixels)}I", *pixels)


class FrameDiffTests(unittest.TestCase):
    def bounds(self, old, new, width, height):
        return frame_diff.changed_bounds(pack(old), pack(new), width, height, width * 4)

    def test_equal_frames_have_no_change(self):
        a = frame(64, 32)
        self.assertIsNone(self.bounds(a, a, 64, 32))
        self.assertFalse(frame_diff.is_significant(None))

    def test_a_caret_blink_is_not_significant(self):
        a = frame(64, 32)
        b = list(a)
        for y in range(5, 25):
            b[y * 64 + 10] = 0xFFFFFFFF
        changed = self.bounds(a, b, 64, 32)
        self.assertEqual(changed, DesktopRect(10, 5, 1, 20))
        self.assertFalse(frame_diff.is_significant(changed))

    def test_a_window_is_significant(self):
        a = frame(64, 32)
        b = list(a)
        for y in range(2, 12):
            for x in range(3, 40):
                b[y * 64 + x] = 0xFFFFFFFF
        changed = self.bounds(a, b, 64, 32)
        self.assertEqual(changed, DesktopRect(3, 2, 37, 10))
        self.assertTrue(frame_diff.is_significant(changed))

    def test_one_byte_change_in_the_last_pixel_and_padded_rows(self):
        width, height, stride = 5, 3, 24  # 4 bytes of padding per row
        old = bytearray(stride * height)
        new = bytearray(old)
        new[2 * stride + 4 * 4 + 3] = 1  # alpha of the last pixel of the last row
        new[2 * stride + 20] = 7  # padding: ignored
        self.assertEqual(frame_diff.changed_bounds(bytes(old), bytes(new), width, height, stride),
                         DesktopRect(4, 2, 1, 1))

    def test_settles_after_quiet_time(self):
        tracker = SettleTracker(settle=0.5, start=0)
        polls = [(True, 0.1), (False, 0.4), (True, 0.5), (False, 0.9), (False, 1.0)]
        self.assertEqual([tracker.update(changed, at) for changed, at in polls], [False, False, False, False, True])


class TextMatchingTests(unittest.TestCase):
    @staticmethod
    def matched(query, lines):
        return [lines[m.line_index][m.start:m.end] for m in text_matching.find(query, lines)]

    def test_finds_case_and_accent_insensitively(self):
        self.assertEqual(self.matched("grüsse", ["Viele Grüsse"]), ["Grüsse"])
        self.assertEqual(self.matched("Datei", ["Bearbeiten", "Datei  Bearbeiten  Ansicht"]), ["Datei"])
        self.assertEqual(self.matched("GRÜSSE", ["grüsse"]), ["grüsse"])
        self.assertEqual(self.matched("Ubersicht", ["Übersicht"]), ["Übersicht"])
        # Decomposed accents (combining marks) stay with their letter.
        self.assertEqual(self.matched("Ubersicht", ["U\u0308bersicht"]), ["U\u0308bersicht"])

    def test_exact_matches_beat_approximate_ones(self):
        matches = text_matching.find("Speichern", ["Speichem unter", "Speichern"])
        self.assertEqual([m.line_index for m in matches], [1])
        self.assertEqual(matches[0].errors, 0)

    def test_tolerates_a_recognition_error(self):
        # OCR often reads "rn" as "m".
        lines = ["Nicht speichem", "Abbrechen"]
        self.assertEqual(self.matched("Nicht speichern", lines), ["Nicht speichem"])
        self.assertEqual(text_matching.find("Nicht speichern", lines)[0].errors, 2)
        self.assertEqual(self.matched("Hilfe", lines), [])

    def test_short_queries_must_match_exactly(self):
        self.assertEqual(self.matched("OK", ["UK", "Abbrechen"]), [])
        self.assertEqual(self.matched("OK", ["Ok"]), ["Ok"])

    def test_look_alikes_and_spaces_do_not_count(self):
        typed = "Grüße ÄÖÜ ß € @ {} \\ |"
        read = "Grüße ÄÖÜ B € @ {\\Il"
        self.assertEqual(self.matched(typed, [read]), ["Grüße ÄÖÜ B € @ {\\I"])
        self.assertEqual(text_matching.find(typed, [read])[0].errors, 1)
        self.assertEqual(self.matched("OK", ["0K"]), ["0K"])
        self.assertEqual(self.matched("Nicht speichern", ["Nichtspeichern"]), ["Nichtspeichern"])
        self.assertEqual(len(self.matched("C:\\Temp\\log 1.txt", ["C:\\Temp\\Iog l.txt"])), 1)

    def test_finds_every_occurrence(self):
        matches = text_matching.find("ab", ["ab cd ab", "xab"])
        self.assertEqual([m.line_index for m in matches], [0, 0, 1])

    def test_ranges_point_into_the_original_text(self):
        line = "Grüße ÄÖÜ ß € @ {} \\ |"
        match = text_matching.find("äöü ß", [line])[0]
        self.assertEqual(line[match.start:match.end], "ÄÖÜ ß")


class AuditEntryTests(unittest.TestCase):
    def test_typed_text_is_logged_by_length_only(self):
        entry = AuditEntry(date=1_790_000_000, client="claude-code 2.1.306", connection="Windows 11 (10.211.55.9)",
                           tool="type_text", arguments={"session": "s1", "text": "geheim123"}, error=None,
                           duration=0.042)
        line = entry.line(ZoneInfo("Europe/Berlin"))
        self.assertTrue(line.startswith('2026-09-21T16:13:20.000+02:00 client="claude-code 2.1.306"'), line)
        self.assertIn('connection="Windows 11 (10.211.55.9)" tool=type_text', line)
        self.assertIn('"text":"<9 characters>"', line)
        self.assertNotIn("geheim", line)
        self.assertTrue(line.endswith(" ok 42ms"))

    def test_errors_are_quoted(self):
        entry = AuditEntry(date=0, client="x", connection=None, tool="connect", arguments={"connection": "Lab"},
                           error='The user denied access to "Lab".', duration=2)
        self.assertEqual(entry.line(ZoneInfo("UTC")),
                         '1970-01-01T00:00:00.000Z client="x" tool=connect args={"connection":"Lab"} '
                         'error="The user denied access to \\"Lab\\"." 2000ms')

    def test_the_log_file_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "agent.log"
            audit = AuditLog(path)
            audit.event("MCP server started")
            audit.record(AuditEntry(date=0, client="x", connection=None, tool="list_sessions"))
            self.assertTrue(audit.flush())
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(lines[0].endswith(" MCP server started"))
            self.assertIn("tool=list_sessions", lines[1])
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class ToolArgumentsTests(unittest.TestCase):
    def test_typed_access_with_readable_errors(self):
        arguments = ToolArguments({"session": "s1", "x": 10.6, "count": 2, "show": "true", "bad": "x",
                                   "flag": True, "none": None})
        self.assertEqual(arguments.string("session"), "s1")
        self.assertEqual(arguments.integer("count", 1, 3), 2)
        self.assertEqual(arguments.integer("x", 0, 100), 11)
        self.assertEqual(arguments.integer("missing", 1, 3, default=1), 1)
        self.assertTrue(arguments.bool("show", False))
        self.assertFalse(arguments.has("none"))
        self.assertEqual(arguments.choice("button", ["left", "right"], "left"), "left")
        with self.assertRaises(ToolArgumentError) as raised:
            arguments.integer("count", 1, 1)
        self.assertEqual(raised.exception.message, "'count' must be between 1 and 1.")
        with self.assertRaises(ToolArgumentError) as raised:
            arguments.string("text")
        self.assertEqual(raised.exception.message, "Missing argument 'text'.")
        with self.assertRaises(ToolArgumentError) as raised:
            arguments.number("bad")
        self.assertEqual(raised.exception.message, "'bad' must be a number.")
        with self.assertRaises(ToolArgumentError):
            arguments.number("flag")  # booleans are not numbers
        with self.assertRaises(ToolArgumentError) as raised:
            arguments.number("x", minimum=0, maximum=5)
        self.assertEqual(raised.exception.message, "'x' must be between 0 and 5.")

    def test_halves_round_away_from_zero(self):
        self.assertEqual(ToolArguments({"n": 2.5}).integer("n", 0, 10), 3)
        self.assertEqual(ToolArguments({"n": -2.5}).integer("n", -10, 10), -3)

    def test_points_in_both_shapes(self):
        arguments = ToolArguments({"a": [[1, 2], [3.5, 4]], "b": [{"x": 1, "y": 2}], "c": [[1]]})
        self.assertEqual(arguments.points("a"), [PixelPoint(1, 2), PixelPoint(3.5, 4)])
        self.assertEqual(arguments.points("b"), [PixelPoint(1, 2)])
        with self.assertRaises(ToolArgumentError):
            arguments.points("c")

    def test_regions(self):
        arguments = ToolArguments({"r": {"x": 1, "y": 2, "width": 30, "height": 40},
                                   "z": {"x": 1, "y": 2, "width": 0, "height": 4}})
        self.assertEqual(arguments.optional_rect("r"), PixelRect(1, 2, 30, 40))
        self.assertIsNone(arguments.optional_rect("none"))
        with self.assertRaises(ToolArgumentError):
            arguments.optional_rect("z")

    def test_string_arrays(self):
        arguments = ToolArguments({"one": "ctrl", "many": ["ctrl", "shift"], "bad": [1]})
        self.assertEqual(arguments.string_array("one"), ["ctrl"])
        self.assertEqual(arguments.string_array("many"), ["ctrl", "shift"])
        self.assertEqual(arguments.string_array("missing"), [])
        with self.assertRaises(ToolArgumentError):
            arguments.string_array("bad")


if __name__ == "__main__":
    unittest.main()
