"""Local key events to RDP input for one session (the Linux counterpart of the KeyboardEngine).

Keys go to Windows as scancodes, so shortcuts, games and the Windows layout work as on a real PC
keyboard; the session's layout follows the local one (layouts.py). Text input modes per connection:

- smart (default): scancodes, except characters typed with AltGr (XKB level 3 and up), which are
  sent as Unicode. Layouts place those symbols differently (on a German Mac layout @ is AltGr+L, on the
  Windows German layout AltGr+Q), so the character the local layout shows is the one that arrives.
  AltGr itself is held back until the next key shows whether it types a character.
- unicode: every printable character typed without Ctrl/Alt/Super as Unicode (for a remote layout that
  differs from the local one); dead keys compose locally.
- scancodes: everything as scancodes, nothing translated.

Shortcuts with Ctrl+Alt+Super stay in the app (tab switching, full screen), like ⌃⌥⌘ in the macOS
version. Ctrl+Alt+End sends Ctrl+Alt+Delete, as in Microsoft's client.
"""

from __future__ import annotations

import unicodedata
from enum import Enum

from gi.repository import Gdk

from . import scancodes
from .scancodes import Scancode


class TextInput(Enum):
    SMART = "smart"
    UNICODE = "unicode"
    SCANCODES = "scancodes"

    @classmethod
    def parse(cls, value: str | None) -> TextInput:
        try:
            return cls(value)
        except ValueError:
            return cls.SMART


_LEVEL3_KEYVALS = frozenset(
    Gdk.keyval_from_name(name)
    for name in ("ISO_Level3_Shift", "ISO_Level3_Latch", "ISO_Level3_Lock", "ISO_Level5_Shift",
                 "ISO_Level5_Latch", "ISO_Level5_Lock", "Mode_switch")
)

# Dead key -> (combining mark, spacing form when nothing composes).
_DEAD_KEYS: dict[str, tuple[str, str]] = {
    "dead_grave": ("̀", "`"),
    "dead_acute": ("́", "´"),
    "dead_circumflex": ("̂", "^"),
    "dead_tilde": ("̃", "~"),
    "dead_macron": ("̄", "¯"),
    "dead_breve": ("̆", "˘"),
    "dead_abovedot": ("̇", "˙"),
    "dead_diaeresis": ("̈", "¨"),
    "dead_abovering": ("̊", "˚"),
    "dead_doubleacute": ("̋", "˝"),
    "dead_caron": ("̌", "ˇ"),
    "dead_cedilla": ("̧", "¸"),
    "dead_ogonek": ("̨", "˛"),
}

_SHORTCUT_MODIFIERS = frozenset({scancodes.LEFT_CTRL, scancodes.RIGHT_CTRL, scancodes.LEFT_ALT,
                                 scancodes.LEFT_WIN, scancodes.RIGHT_WIN})

# What a pressed key sent, so its release matches.
_UNICODE = "unicode"
_DEFERRED = "deferred"


def printable_character(keyval: int) -> str | None:
    """The character a keyval types, or None for control and function keys."""
    code = Gdk.keyval_to_unicode(keyval)
    if not code:
        return None
    char = chr(code)
    return None if unicodedata.category(char).startswith("C") else char


def compose(dead: str, char: str) -> str:
    """Applies a dead key to the next character: ´ + e = é; without a composed form both are typed."""
    mark, spacing = _DEAD_KEYS[dead]
    if char == " ":
        return spacing
    composed = unicodedata.normalize("NFC", char + mark)
    return composed if len(composed) == 1 else spacing + char


def is_app_shortcut(state: Gdk.ModifierType) -> bool:
    """Ctrl+Alt+Super + key belongs to Weitblick Remote, never to the session."""
    wanted = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK
    return (state & wanted) == wanted


class SessionKeyboard:
    """Turns GTK key events of one session into scancodes and Unicode input. All on the main loop."""

    def __init__(self, session, text_input: TextInput = TextInput.SMART, layout_id: int = 0x0409):
        self.session = session
        self.text_input = text_input
        #: The session's Windows layout, for chords (menu items, agent keys).
        self.layout_id = layout_id
        self._down: dict[int, object] = {}  # keycode -> Scancode | _UNICODE | _DEFERRED
        self._sent: set[Scancode] = set()  # scancodes down on the remote
        self._level3: Scancode | None = None  # held back AltGr (not sent yet)
        self._level3_sent = False
        self._dead: str | None = None

    # MARK: Events

    def handle(self, keycode: int, keyval: int, state: Gdk.ModifierType, level: int, pressed: bool) -> bool:
        """Sends one key event; returns True when it was consumed (always, for keys of the session)."""
        if pressed:
            self._press(keycode, keyval, level)
        else:
            self._release(keycode)
        return True

    def _press(self, keycode: int, keyval: int, level: int) -> None:
        scancode = scancodes.from_xkb_keycode(keycode)
        if self.text_input is not TextInput.SCANCODES and keyval in _LEVEL3_KEYVALS and scancode is not None:
            # Held back: the next key decides between a character (Unicode) and AltGr + key.
            if keycode not in self._down:
                self._down[keycode] = _DEFERRED
                self._level3 = scancode
                self._level3_sent = False
            return

        modifiers_held = bool(self._sent & _SHORTCUT_MODIFIERS)
        level3_held = self._level3 is not None
        wants_text = self.text_input is TextInput.UNICODE or (
            self.text_input is TextInput.SMART and (level >= 2 or level3_held))
        name = Gdk.keyval_name(keyval) or ""
        if wants_text and not modifiers_held and name in _DEAD_KEYS:
            self._dead = name
            self._down[keycode] = _UNICODE
            return
        char = printable_character(keyval)
        if wants_text and not modifiers_held and char is not None:
            if self._dead:
                char = compose(self._dead, char)
                self._dead = None
            self.session.send_text(char)
            self._down[keycode] = _UNICODE
            return

        self._dead = None
        if scancode is None:
            return
        self._flush_level3()
        if scancode == scancodes.PAUSE:
            if self._sent & {scancodes.LEFT_CTRL, scancodes.RIGHT_CTRL}:
                self.session.send_scancode(0x46, True, True)  # Break
                self.session.send_scancode(0x46, True, False)
            else:
                self.session.send_pause()
            return
        if scancode == scancodes.END and self._ctrl_alt_held():
            scancode = scancodes.DELETE  # Ctrl+Alt+End = Ctrl+Alt+Delete, as in mstsc
        self._send(scancode, True)
        self._down[keycode] = scancode

    def _release(self, keycode: int) -> None:
        sent = self._down.pop(keycode, None)
        if sent is _DEFERRED:
            if self._level3_sent and self._level3 is not None:
                self._send(self._level3, False)
            self._level3 = None
            self._level3_sent = False
        elif isinstance(sent, Scancode):
            self._send(sent, False)
        elif sent is None:
            # Pressed before the session had the focus: release it remotely anyway (no-op if up).
            scancode = scancodes.from_xkb_keycode(keycode)
            if scancode is not None and scancode != scancodes.PAUSE:
                self._send(scancode, False)

    def _flush_level3(self) -> None:
        if self._level3 is not None and not self._level3_sent:
            self._send(self._level3, True)
            self._level3_sent = True

    def _ctrl_alt_held(self) -> bool:
        ctrl = self._sent & {scancodes.LEFT_CTRL, scancodes.RIGHT_CTRL}
        alt = self._sent & {scancodes.LEFT_ALT, scancodes.RIGHT_ALT}
        return bool(ctrl and alt)

    def _send(self, scancode: Scancode, down: bool) -> None:
        if down:
            self._sent.add(scancode)
        else:
            self._sent.discard(scancode)
        self.session.send_scancode(scancode.code, scancode.extended, down)

    # MARK: Focus

    def focus_gained(self, caps_lock: bool, num_lock: bool, scroll_lock: bool) -> None:
        """The session got the keyboard: nothing is down, the lock keys match the local ones."""
        self._reset()
        self.session.release_all_keys()
        self.session.send_sync(caps_lock, num_lock, scroll_lock)

    def focus_lost(self) -> None:
        """Window inactive, tab switch, agent takes over: let go of every key (no stuck modifiers)."""
        self._reset()
        self.session.release_all_keys()

    def app_shortcut_used(self) -> None:
        """A Ctrl+Alt+Super shortcut went to the app: the modifiers already sent must not stay down."""
        self.session.release_all_keys()
        self._sent.clear()
        for keycode, sent in list(self._down.items()):
            if isinstance(sent, Scancode):
                del self._down[keycode]

    def _reset(self) -> None:
        self._down.clear()
        self._sent.clear()
        self._level3 = None
        self._level3_sent = False
        self._dead = None

    # MARK: Chords

    def tap(self, notation: str) -> bool:
        """Presses a Windows chord like "ctrl+alt+delete" or "win" through the session's layout."""
        from . import chords
        actions = chords.tap_actions(chords.parse_chord(notation), self.layout_id)
        if not actions:
            return False
        chords.send_actions(self.session, actions)
        return True
