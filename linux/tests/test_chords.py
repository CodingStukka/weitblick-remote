import unittest

from weitblick.keyboard import windows_layouts as layouts
from weitblick.keyboard.chords import (ChordNotationError, KeyAction, WindowsModifier, parse_chord, send_actions,
                                       tap_actions)


def down(code, extended=False):
    return KeyAction.scancode(code, extended, True)


def up(code, extended=False):
    return KeyAction.scancode(code, extended, False)


class ParseChordTests(unittest.TestCase):
    def test_notation_round_trips(self):
        self.assertEqual(str(parse_chord("ctrl+alt+delete")), "ctrl+alt+delete")
        self.assertEqual(str(parse_chord("shift+CTRL+Esc")), "ctrl+shift+escape")
        self.assertEqual(str(parse_chord("win")), "win")
        self.assertEqual(str(parse_chord("ctrl+plus")), "ctrl+plus")
        self.assertEqual(str(parse_chord("enter")), "return")
        self.assertEqual(str(parse_chord("apps")), "menu")
        self.assertEqual(parse_chord("alt+tab").modifiers, WindowsModifier.ALT)

    def test_rejects_bad_notation(self):
        for notation in ["", "ctrl+", "+a", "ctrl+banana", "cmd+c", "hyper+a", "ctrl+*", "a+b"]:
            with self.subTest(notation=notation), self.assertRaises(ChordNotationError):
                parse_chord(notation)


class TapActionTests(unittest.TestCase):
    def test_ctrl_alt_delete(self):
        self.assertEqual(tap_actions(parse_chord("ctrl+alt+delete"), layouts.GERMAN),
                         [down(0x1D), down(0x38), down(0x53, True), up(0x53, True), up(0x38), up(0x1D)])

    def test_modifier_tap(self):
        self.assertEqual(tap_actions(parse_chord("win"), layouts.US_ENGLISH), [down(0x5B, True), up(0x5B, True)])

    def test_pause_and_printscreen(self):
        self.assertEqual(tap_actions(parse_chord("pause"), layouts.US_ENGLISH), [KeyAction("pause")])
        self.assertEqual(tap_actions(parse_chord("printscreen"), layouts.US_ENGLISH),
                         [down(0x37, True), up(0x37, True)])
        self.assertEqual(tap_actions(parse_chord("f24"), layouts.US_ENGLISH), [down(0x76), up(0x76)])

    def test_letters_follow_the_layout(self):
        z = parse_chord("ctrl+z")
        self.assertEqual(tap_actions(z, layouts.GERMAN), [down(0x1D), down(0x15), up(0x15), up(0x1D)])
        self.assertEqual(tap_actions(z, layouts.AUSTRIAN)[1], down(0x15))
        self.assertEqual(tap_actions(z, layouts.SWISS_GERMAN)[1], down(0x15))
        self.assertEqual(tap_actions(z, layouts.US_ENGLISH)[1], down(0x2C))
        self.assertEqual(tap_actions(z, layouts.FRENCH)[1], down(0x11))
        self.assertEqual(tap_actions(parse_chord("ctrl+a"), layouts.FRENCH)[1], down(0x10))
        self.assertEqual(tap_actions(parse_chord("ctrl+m"), layouts.FRENCH)[1], down(0x27))
        # Unknown layouts and variants of known ones.
        self.assertEqual(tap_actions(z, 0x0419)[1], down(0x2C))
        self.assertEqual(tap_actions(z, 0x00010407)[1], down(0x15))

    def test_symbols_need_an_unmodified_key(self):
        self.assertEqual(tap_actions(parse_chord("ctrl+plus"), layouts.GERMAN)[1], down(0x1B))
        self.assertEqual(tap_actions(parse_chord("ctrl+-"), layouts.US_ENGLISH)[1], down(0x0C))
        self.assertEqual(tap_actions(parse_chord("ctrl+@"), layouts.GERMAN), [])
        self.assertEqual(tap_actions(parse_chord("ctrl+@"), layouts.US_ENGLISH), [])
        # Characters the layout lacks resolve to their US position, like Windows shortcuts.
        self.assertEqual(tap_actions(parse_chord("ctrl+["), layouts.GERMAN)[1], down(0x1A))

    def test_send_actions_calls_the_session(self):
        calls = []

        class Session:
            def send_scancode(self, code, extended, down):
                calls.append(("scancode", code, extended, down))

            def send_pause(self):
                calls.append(("pause",))

            def send_unicode_unit(self, unit, down):
                calls.append(("unicode", unit, down))

        send_actions(Session(), [down(0x1D), KeyAction("pause"), KeyAction.unicode(0x20AC, True), up(0x1D)])
        self.assertEqual(calls, [("scancode", 0x1D, False, True), ("pause",), ("unicode", 0x20AC, True),
                                 ("scancode", 0x1D, False, False)])


class WindowsLayoutTests(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(layouts.key_for_character(layouts.GERMAN, "@"), layouts.KeyPosition(0x10, altgr=True))
        self.assertEqual(layouts.key_for_character(layouts.GERMAN, "€"), layouts.KeyPosition(0x12, altgr=True))
        self.assertEqual(layouts.key_for_character(layouts.GERMAN, "Z"), layouts.KeyPosition(0x15, shift=True))
        self.assertEqual(layouts.key_for_character(layouts.GERMAN, "|"), layouts.KeyPosition(0x56, altgr=True))
        self.assertEqual(layouts.key_for_character(layouts.GERMAN, "^"), layouts.KeyPosition(0x29, dead=True))
        self.assertEqual(layouts.key_for_character(layouts.US_ENGLISH, "@"), layouts.KeyPosition(0x03, shift=True))
        self.assertEqual(layouts.key_for_character(layouts.SWISS_GERMAN, "@"), layouts.KeyPosition(0x03, altgr=True))
        self.assertEqual(layouts.key_for_character(layouts.UK_ENGLISH, "@"), layouts.KeyPosition(0x28, shift=True))
        self.assertEqual(layouts.key_for_character(layouts.FRENCH, "1"), layouts.KeyPosition(0x02, shift=True))
        self.assertEqual(layouts.key_for_character(layouts.SPANISH, "ñ"), layouts.KeyPosition(0x27))
        self.assertEqual(layouts.key_for_character(layouts.ITALIAN, "@"), layouts.KeyPosition(0x27, altgr=True))
        self.assertEqual(layouts.key_for_character(layouts.US_ENGLISH, " "), layouts.KeyPosition(0x39))
        self.assertIsNone(layouts.key_for_character(layouts.US_ENGLISH, "€"))

    def test_every_layout_has_letters_and_digits(self):
        for layout in (layouts.US_ENGLISH, layouts.GERMAN, layouts.SWISS_GERMAN, layouts.UK_ENGLISH,
                       layouts.FRENCH, layouts.SPANISH, layouts.ITALIAN):
            for character in "abcdefghijklmnopqrstuvwxyz0123456789":
                with self.subTest(layout=hex(layout), character=character):
                    self.assertIsNotNone(layouts.key_for_character(layout, character))
                    self.assertIsNotNone(layouts.base_key(layout, character))

    def test_has_table(self):
        self.assertTrue(layouts.has_table(0x0407))
        self.assertTrue(layouts.has_table(0x00010407))
        self.assertFalse(layouts.has_table(0x0419))


if __name__ == "__main__":
    unittest.main()
