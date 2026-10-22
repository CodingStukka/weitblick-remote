"""Linux key codes to RDP scancodes (PC/AT set 1 make codes plus the E0 "extended" flag).

GTK reports XKB key codes, which are Linux evdev codes + 8 on X11 and Wayland alike. evdev codes 1…88
are the set-1 scancodes themselves (the kernel took them over from the PC keyboard); everything above
needs a table. Pause has no scancode of its own (it is an E1 sequence, see RDPSession.send_pause).
"""

from __future__ import annotations

from typing import NamedTuple

#: XKB key code = evdev code + 8.
XKB_OFFSET = 8


class Scancode(NamedTuple):
    code: int
    extended: bool = False


#: Marks the Pause key (sent as its own E1 sequence, or as Break (0x46 extended) with Ctrl).
PAUSE = Scancode(0x45, True)

# evdev codes above 88 (linux/input-event-codes.h) and their set-1 scancodes.
_EVDEV_HIGH: dict[int, Scancode] = {
    85: Scancode(0x76),         # KEY_ZENKAKUHANKAKU
    86: Scancode(0x56),         # KEY_102ND: < > | on ISO keyboards
    87: Scancode(0x57),         # F11
    88: Scancode(0x58),         # F12
    89: Scancode(0x73),         # KEY_RO (Japanese \ _)
    90: Scancode(0x78),         # KEY_KATAKANA
    91: Scancode(0x77),         # KEY_HIRAGANA
    92: Scancode(0x79),         # KEY_HENKAN
    93: Scancode(0x70),         # KEY_KATAKANAHIRAGANA
    94: Scancode(0x7B),         # KEY_MUHENKAN
    95: Scancode(0x5C),         # KEY_KPJPCOMMA
    96: Scancode(0x1C, True),   # keypad Enter
    97: Scancode(0x1D, True),   # right Ctrl
    98: Scancode(0x35, True),   # keypad /
    99: Scancode(0x37, True),   # Print Screen (SysRq)
    100: Scancode(0x38, True),  # right Alt / AltGr
    102: Scancode(0x47, True),  # Home
    103: Scancode(0x48, True),  # Up
    104: Scancode(0x49, True),  # Page Up
    105: Scancode(0x4B, True),  # Left
    106: Scancode(0x4D, True),  # Right
    107: Scancode(0x4F, True),  # End
    108: Scancode(0x50, True),  # Down
    109: Scancode(0x51, True),  # Page Down
    110: Scancode(0x52, True),  # Insert
    111: Scancode(0x53, True),  # Delete
    113: Scancode(0x20, True),  # Mute
    114: Scancode(0x2E, True),  # Volume down
    115: Scancode(0x30, True),  # Volume up
    116: Scancode(0x5E, True),  # Power
    117: Scancode(0x59),        # keypad =
    119: PAUSE,                 # Pause
    121: Scancode(0x7E),        # keypad , (Brazilian)
    122: Scancode(0x72),        # KEY_HANGEUL
    123: Scancode(0x71),        # KEY_HANJA
    124: Scancode(0x7D),        # KEY_YEN
    125: Scancode(0x5B, True),  # left Super = left Windows key
    126: Scancode(0x5C, True),  # right Super
    127: Scancode(0x5D, True),  # Menu (Compose) = application key
    128: Scancode(0x68, True),  # Stop (browser)
    140: Scancode(0x21, True),  # Calculator
    142: Scancode(0x5F, True),  # Sleep
    143: Scancode(0x63, True),  # Wake up
    155: Scancode(0x6C, True),  # Mail
    156: Scancode(0x66, True),  # Bookmarks (browser favorites)
    157: Scancode(0x6B, True),  # Computer (My Computer)
    158: Scancode(0x6A, True),  # Back (browser)
    159: Scancode(0x69, True),  # Forward (browser)
    163: Scancode(0x19, True),  # Next track
    164: Scancode(0x22, True),  # Play/Pause
    165: Scancode(0x10, True),  # Previous track
    166: Scancode(0x24, True),  # Stop media
    172: Scancode(0x32, True),  # Home page (browser)
    173: Scancode(0x67, True),  # Refresh (browser)
    183: Scancode(0x64),        # F13
    184: Scancode(0x65),        # F14
    185: Scancode(0x66),        # F15
    186: Scancode(0x67),        # F16
    187: Scancode(0x68),        # F17
    188: Scancode(0x69),        # F18
    189: Scancode(0x6A),        # F19
    190: Scancode(0x6B),        # F20
    191: Scancode(0x6C),        # F21
    192: Scancode(0x6D),        # F22
    193: Scancode(0x6E),        # F23
    194: Scancode(0x76),        # F24
    217: Scancode(0x65, True),  # Search (browser)
    226: Scancode(0x6D, True),  # Media select
}

#: Scancodes of the modifier keys, for state tracking and deferral.
LEFT_CTRL = Scancode(0x1D)
RIGHT_CTRL = Scancode(0x1D, True)
LEFT_SHIFT = Scancode(0x2A)
RIGHT_SHIFT = Scancode(0x36)
LEFT_ALT = Scancode(0x38)
RIGHT_ALT = Scancode(0x38, True)
LEFT_WIN = Scancode(0x5B, True)
RIGHT_WIN = Scancode(0x5C, True)
CAPS_LOCK = Scancode(0x3A)
NUM_LOCK = Scancode(0x45)
SCROLL_LOCK = Scancode(0x46)
DELETE = Scancode(0x53, True)
END = Scancode(0x4F, True)

MODIFIERS = frozenset({LEFT_CTRL, RIGHT_CTRL, LEFT_SHIFT, RIGHT_SHIFT, LEFT_ALT, RIGHT_ALT, LEFT_WIN, RIGHT_WIN})


def from_evdev(code: int) -> Scancode | None:
    """The scancode of an evdev key code, or None for keys Windows has no scancode for."""
    if 1 <= code <= 84:
        # 84 is unused by the kernel, but keeping the range simple costs nothing.
        return Scancode(code) if code != 84 else None
    return _EVDEV_HIGH.get(code)


def from_xkb_keycode(keycode: int) -> Scancode | None:
    """The scancode of a GDK/XKB hardware key code (evdev + 8)."""
    return from_evdev(keycode - XKB_OFFSET)
