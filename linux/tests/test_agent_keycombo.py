import unittest

from weitblick.agent import keycombo
from weitblick.agent.errors import ToolArgumentError
from weitblick.agent.text_typing import HeldModifiers, typing_actions
from weitblick.keyboard import windows_layouts as layouts
from weitblick.keyboard.chords import KeyAction


def down(code, extended=False):
    return KeyAction.scancode(code, extended, True)


def up(code, extended=False):
    return KeyAction.scancode(code, extended, False)


def press(keys, layout, repeat=1):
    return keycombo.actions(keycombo.parse(keys), layout, repeat)


class KeyComboTests(unittest.TestCase):
    def test_parses_common_notations(self):
        names = lambda notation: [str(chord) for chord in keycombo.parse(notation)]  # noqa: E731
        self.assertEqual(names("ctrl+shift+esc"), ["ctrl+shift+escape"])
        self.assertEqual(names("Win+R"), ["win+r"])
        self.assertEqual(names("alt+F4"), ["alt+f4"])
        self.assertEqual(names("Enter"), ["return"])
        self.assertEqual(names("super+e"), ["win+e"])
        self.assertEqual(names("ctrl+a  ctrl+c"), ["ctrl+a", "ctrl+c"])
        self.assertEqual(names("pgdn arrowleft PageUp"), ["pagedown", "left", "pageup"])
        self.assertEqual(names("ctrl++ ctrl+minus"), ["ctrl+plus", "ctrl+-"])
        self.assertEqual(names("win"), ["win"])
        self.assertEqual(names("altgr+q"), ["ctrl+alt+q"])

    def test_refuses_mac_and_unknown_keys(self):
        for notation in ["cmd+c", "ctrl+banana", "ctrl+", "   ", "hyper+a"]:
            with self.subTest(notation=notation), self.assertRaises(ToolArgumentError):
                keycombo.parse(notation)

    def test_ctrl_z_presses_the_key_that_types_z(self):
        # German: z is on the physical Y key (scancode 0x15); US: on 0x2C.
        self.assertEqual(press("ctrl+z", layouts.GERMAN), [down(0x1D), down(0x15), up(0x15), up(0x1D)])
        self.assertEqual(press("ctrl+z", layouts.US_ENGLISH), [down(0x1D), down(0x2C), up(0x2C), up(0x1D)])
        self.assertEqual(press("ctrl+y", layouts.GERMAN), [down(0x1D), down(0x2C), up(0x2C), up(0x1D)])

    def test_plus_on_german_is_the_key_right_of_u_umlaut(self):
        self.assertEqual(press("ctrl++", layouts.GERMAN), [down(0x1D), down(0x1B), up(0x1B), up(0x1D)])

    def test_windows_and_function_keys(self):
        self.assertEqual(press("win+r", layouts.GERMAN),
                         [down(0x5B, True), down(0x13), up(0x13), up(0x5B, True)])
        self.assertEqual(press("alt+f4", layouts.GERMAN), [down(0x38), down(0x3E), up(0x3E), up(0x38)])
        self.assertEqual(press("enter", layouts.GERMAN), [down(0x1C), up(0x1C)])
        self.assertEqual(press("ctrl+shift+esc", layouts.GERMAN),
                         [down(0x1D), down(0x2A), down(0x01), up(0x01), up(0x2A), up(0x1D)])

    def test_sequences_and_repeats(self):
        self.assertEqual(press("ctrl+a ctrl+c", layouts.GERMAN),
                         [down(0x1D), down(0x1E), up(0x1E), up(0x1D), down(0x1D), down(0x2E), up(0x2E), up(0x1D)])
        self.assertEqual(press("tab", layouts.US_ENGLISH, repeat=3), [down(0x0F), up(0x0F)] * 3)

    def test_characters_without_a_key_are_refused(self):
        # '@' needs AltGr on German: there is no unmodified key for it.
        with self.assertRaises(ToolArgumentError):
            press("ctrl+@", layouts.GERMAN)


class TextTypingTests(unittest.TestCase):
    @staticmethod
    def units(groups):
        result = []
        for group in groups:
            parts = []
            for action in group:
                if action.kind == "unicode" and action.down:
                    parts.append(f"U+{action.code:04X}")
                elif action.kind == "scancode" and action.down:
                    parts.append(f"SC{action.code:02X}")
            result.append(" ".join(parts))
        return result

    def test_text_is_unicode_and_line_breaks_are_enter(self):
        self.assertEqual(self.units(typing_actions("aß\n€")), ["U+0061", "U+00DF", "SC1C", "U+20AC"])
        self.assertEqual(self.units(typing_actions("a\r\nb\tc")), ["U+0061", "SC1C", "U+0062", "SC0F", "U+0063"])
        self.assertEqual(self.units(typing_actions("x\r")), ["U+0078", "SC1C"])

    def test_every_down_has_its_up(self):
        for group in typing_actions("Grüße ÄÖÜ ß € @ {} \\ | 😀\n"):
            self.assertEqual(len(group) % 2, 0)
            for index in range(0, len(group), 2):
                first, second = group[index], group[index + 1]
                self.assertEqual((first.kind, first.code, first.down), (second.kind, second.code, True))
                self.assertFalse(second.down)

    def test_emoji_is_a_surrogate_pair(self):
        self.assertEqual(self.units(typing_actions("😀")), ["U+D83D U+DE00"])

    def test_control_characters_are_dropped(self):
        self.assertEqual(typing_actions("\x07\x1b"), [])

    def test_click_modifiers(self):
        held = HeldModifiers.parse(["ctrl", "Shift", "ctrl"])
        self.assertEqual(HeldModifiers.press(held), [down(0x1D), down(0x2A)])
        self.assertEqual(HeldModifiers.release(held), [up(0x2A), up(0x1D)])
        self.assertEqual(HeldModifiers.parse(["win"]), [(0x5B, True)])
        with self.assertRaises(ToolArgumentError):
            HeldModifiers.parse(["cmd"])


if __name__ == "__main__":
    unittest.main()
