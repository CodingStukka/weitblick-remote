"""Scancode mapping, layout detection and the session keyboard (with a fake session)."""

import unittest

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk  # noqa: E402

from weitblick.keyboard import layouts, scancodes  # noqa: E402
from weitblick.keyboard.scancodes import Scancode  # noqa: E402
from weitblick.keyboard.session_keyboard import SessionKeyboard, TextInput, compose, is_app_shortcut  # noqa: E402

NONE = Gdk.ModifierType(0)


def keyval(name: str) -> int:
    return Gdk.keyval_from_name(name)


class FakeSession:
    def __init__(self):
        self.events = []

    def send_scancode(self, code, extended, down):
        self.events.append(("sc", code, extended, down))

    def send_pause(self):
        self.events.append(("pause",))

    def send_text(self, text):
        self.events.append(("text", text))

    def send_unicode_unit(self, unit, down):
        self.events.append(("u", unit, down))

    def send_sync(self, caps, num, scroll):
        self.events.append(("sync", caps, num, scroll))

    def release_all_keys(self):
        self.events.append(("release-all",))


# XKB key codes (evdev + 8) of a few keys.
KEY_A, KEY_L, KEY_Q, KEY_E = 38, 46, 24, 26
KEY_RALT, KEY_LCTRL, KEY_LALT, KEY_END, KEY_PAUSE, KEY_SHIFT = 108, 37, 64, 115, 127, 50


class ScancodeTests(unittest.TestCase):
    def test_low_codes_are_set1(self):
        self.assertEqual(scancodes.from_xkb_keycode(9), Scancode(0x01))  # Esc
        self.assertEqual(scancodes.from_xkb_keycode(KEY_A), Scancode(0x1E))
        self.assertEqual(scancodes.from_xkb_keycode(KEY_SHIFT), Scancode(0x2A))

    def test_extended_keys(self):
        self.assertEqual(scancodes.from_xkb_keycode(KEY_RALT), Scancode(0x38, True))
        self.assertEqual(scancodes.from_evdev(103), Scancode(0x48, True))  # Up
        self.assertEqual(scancodes.from_evdev(96), Scancode(0x1C, True))  # keypad Enter
        self.assertEqual(scancodes.from_evdev(125), Scancode(0x5B, True))  # Super
        self.assertEqual(scancodes.from_evdev(86), Scancode(0x56))  # ISO < >

    def test_unknown_keys(self):
        self.assertIsNone(scancodes.from_evdev(0))
        self.assertIsNone(scancodes.from_evdev(84))
        self.assertIsNone(scancodes.from_evdev(500))


class LayoutTests(unittest.TestCase):
    def test_xkb_names(self):
        self.assertEqual(layouts.klid_for_xkb("de"), 0x0407)
        self.assertEqual(layouts.klid_for_xkb("de", "mac_nodeadkeys"), 0x0407)
        self.assertEqual(layouts.klid_for_xkb("ch", "fr"), 0x100C)
        self.assertEqual(layouts.klid_for_xkb("us", "intl"), 0x20409)
        self.assertIsNone(layouts.klid_for_xkb("xx"))

    def test_gnome_input_source(self):
        self.assertEqual(layouts.parse_input_source("de+mac_nodeadkeys"), ("de", "mac_nodeadkeys"))
        self.assertEqual(layouts.parse_input_source("us"), ("us", ""))

    def test_known_layouts_are_unique(self):
        ids = [layout.klid for layout in layouts.known_layouts()]
        self.assertEqual(len(ids), len(set(ids)))


class ComposeTests(unittest.TestCase):
    def test_compose(self):
        self.assertEqual(compose("dead_acute", "e"), "é")
        self.assertEqual(compose("dead_circumflex", "a"), "â")
        self.assertEqual(compose("dead_diaeresis", "U"), "Ü")
        self.assertEqual(compose("dead_acute", " "), "´")
        self.assertEqual(compose("dead_acute", "x"), "´x")


class SessionKeyboardTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.keyboard = SessionKeyboard(self.session, TextInput.SMART, 0x0407)

    def press(self, keycode, name, level=0):
        self.keyboard.handle(keycode, keyval(name), NONE, level, True)

    def release(self, keycode, name, level=0):
        self.keyboard.handle(keycode, keyval(name), NONE, level, False)

    def test_plain_key_is_a_scancode(self):
        self.press(KEY_A, "a")
        self.release(KEY_A, "a")
        self.assertEqual(self.session.events, [("sc", 0x1E, False, True), ("sc", 0x1E, False, False)])

    def test_altgr_character_goes_as_unicode(self):
        # German Mac layout: AltGr+L types @ (Windows German has it on AltGr+Q).
        self.press(KEY_RALT, "ISO_Level3_Shift")
        self.press(KEY_L, "at", level=2)
        self.release(KEY_L, "at", level=2)
        self.release(KEY_RALT, "ISO_Level3_Shift")
        self.assertEqual(self.session.events, [("text", "@")])

    def test_altgr_with_a_function_key_sends_altgr(self):
        self.press(KEY_RALT, "ISO_Level3_Shift")
        self.press(KEY_END, "End")
        self.release(KEY_END, "End")
        self.release(KEY_RALT, "ISO_Level3_Shift")
        self.assertEqual(self.session.events, [
            ("sc", 0x38, True, True), ("sc", 0x4F, True, True), ("sc", 0x4F, True, False), ("sc", 0x38, True, False)])

    def test_scancode_mode_sends_altgr_at_once(self):
        self.keyboard.text_input = TextInput.SCANCODES
        self.press(KEY_RALT, "ISO_Level3_Shift")
        self.press(KEY_L, "at", level=2)
        self.assertEqual(self.session.events, [("sc", 0x38, True, True), ("sc", 0x26, False, True)])

    def test_unicode_mode_types_characters(self):
        self.keyboard.text_input = TextInput.UNICODE
        self.press(KEY_A, "a")
        self.release(KEY_A, "a")
        self.assertEqual(self.session.events, [("text", "a")])

    def test_unicode_mode_keeps_shortcuts(self):
        self.keyboard.text_input = TextInput.UNICODE
        self.press(KEY_LCTRL, "Control_L")
        self.press(KEY_A, "a")
        self.assertEqual(self.session.events, [("sc", 0x1D, False, True), ("sc", 0x1E, False, True)])

    def test_unicode_mode_composes_dead_keys(self):
        self.keyboard.text_input = TextInput.UNICODE
        self.press(21, "dead_acute")
        self.release(21, "dead_acute")
        self.press(KEY_E, "e")
        self.assertEqual(self.session.events, [("text", "é")])

    def test_ctrl_alt_end_is_ctrl_alt_delete(self):
        self.press(KEY_LCTRL, "Control_L")
        self.press(KEY_LALT, "Alt_L")
        self.press(KEY_END, "End")
        self.release(KEY_END, "End")
        self.assertIn(("sc", 0x53, True, True), self.session.events)
        self.assertIn(("sc", 0x53, True, False), self.session.events)
        self.assertNotIn(("sc", 0x4F, True, True), self.session.events)

    def test_pause_and_break(self):
        self.press(KEY_PAUSE, "Pause")
        self.assertEqual(self.session.events, [("pause",)])
        self.session.events.clear()
        self.press(KEY_LCTRL, "Control_L")
        self.press(KEY_PAUSE, "Pause")
        self.assertEqual(self.session.events[1:], [("sc", 0x46, True, True), ("sc", 0x46, True, False)])

    def test_release_without_press_is_sent(self):
        # Pressed before the session had the focus.
        self.release(KEY_A, "a")
        self.assertEqual(self.session.events, [("sc", 0x1E, False, False)])

    def test_focus(self):
        self.press(KEY_A, "a")
        self.keyboard.focus_lost()
        self.keyboard.focus_gained(False, True, False)
        self.assertEqual(self.session.events[1:], [("release-all",), ("release-all",), ("sync", False, True, False)])

    def test_app_shortcut(self):
        mask = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK
        self.assertTrue(is_app_shortcut(mask))
        self.assertFalse(is_app_shortcut(Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK))


if __name__ == "__main__":
    unittest.main()
