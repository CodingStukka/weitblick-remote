"""Windows key combinations in shortcut notation ("ctrl+alt+delete", "win+r", "printscreen") and the
RDP key events that press them. Port of KeyboardEngine's WindowsChord, NamedKey and RDPKeyAction.

    actions = tap_actions(parse_chord("ctrl+z"), layout_id=0x0407)
    send_actions(session, actions)

Characters resolve through the session's Windows layout (windows_layouts), so "ctrl+z" presses the
key that types "z" there.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag
from typing import Protocol

from . import windows_layouts


class WindowsModifier(IntFlag):
    CONTROL = 1
    ALT = 2
    SHIFT = 4
    WINDOWS = 8


#: Press order (left keys); releases go in reverse. (scancode, extended)
_MODIFIER_KEYS: list[tuple[WindowsModifier, int, bool]] = [
    (WindowsModifier.CONTROL, 0x1D, False),
    (WindowsModifier.ALT, 0x38, False),
    (WindowsModifier.SHIFT, 0x2A, False),
    (WindowsModifier.WINDOWS, 0x5B, True),
]

_FUNCTION_KEYS = [0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0x41, 0x42, 0x43, 0x44, 0x57, 0x58,
                  0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x76]

PAUSE = "pause"

#: Keys without a character, by notation name: (scancode, extended), or PAUSE (it has no make/break
#: pair; the bridge sends its E1 sequence).
NAMED_KEYS: dict[str, tuple[int, bool] | str] = {
    "escape": (0x01, False),
    "tab": (0x0F, False),
    "space": (0x39, False),
    "return": (0x1C, False),
    "backspace": (0x0E, False),
    "delete": (0x53, True),
    "insert": (0x52, True),
    "home": (0x47, True),
    "end": (0x4F, True),
    "pageup": (0x49, True),
    "pagedown": (0x51, True),
    "left": (0x4B, True),
    "right": (0x4D, True),
    "up": (0x48, True),
    "down": (0x50, True),
    "keypadenter": (0x1C, True),
    "menu": (0x5D, True),
    "printscreen": (0x37, True),
    "scrolllock": (0x46, False),
    "pause": PAUSE,
    "numlock": (0x45, False),
    "capslock": (0x3A, False),
    **{f"f{number}": (code, False) for number, code in enumerate(_FUNCTION_KEYS, start=1)},
}

_NAMED_ALIASES = {"esc": "escape", "enter": "return", "del": "delete", "forwarddelete": "delete",
                  "help": "insert", "apps": "menu"}

_WINDOWS_MODIFIER_NAMES = {"ctrl": WindowsModifier.CONTROL, "control": WindowsModifier.CONTROL,
                           "alt": WindowsModifier.ALT, "shift": WindowsModifier.SHIFT,
                           "win": WindowsModifier.WINDOWS, "windows": WindowsModifier.WINDOWS}

# Every modifier name, Mac ones included: as the last token they mean "tap the modifiers".
_MODIFIER_NAMES = {"cmd", "command", "⌘", "opt", "option", "alt", "⌥", "ctrl", "control", "⌃", "shift", "⇧",
                   "win", "windows"}


class ChordNotationError(ValueError):
    def __init__(self, notation: str, reason: str):
        super().__init__(f"Invalid shortcut '{notation}': {reason}")
        self.notation = notation
        self.reason = reason


@dataclass(frozen=True)
class KeyRef:
    """A key: kind "character" (the key typing `value`, lowercase) or "named" (a NAMED_KEYS name)."""

    kind: str
    value: str

    @property
    def notation(self) -> str:
        if self.kind == "character" and self.value == "+":
            return "plus"
        return self.value


@dataclass(frozen=True)
class WindowsChord:
    """A Windows key combination such as ctrl+y, or a modifier tap such as win (key None)."""

    modifiers: WindowsModifier
    key: KeyRef | None

    def __str__(self) -> str:
        parts = [name for flag, name in ((WindowsModifier.CONTROL, "ctrl"), (WindowsModifier.ALT, "alt"),
                                         (WindowsModifier.SHIFT, "shift"), (WindowsModifier.WINDOWS, "win"))
                 if self.modifiers & flag]
        if self.key is not None:
            parts.append(self.key.notation)
        return "+".join(parts)


@dataclass(frozen=True)
class KeyAction:
    """One RDP keyboard event, in sending order. kind "scancode" (set-1 `code`, `extended` = E0),
    "unicode" (`code` is one UTF-16 unit) or "pause" (the whole Pause sequence)."""

    kind: str
    code: int = 0
    extended: bool = False
    down: bool = False

    @staticmethod
    def scancode(code: int, extended: bool = False, down: bool = True) -> KeyAction:
        return KeyAction("scancode", code, extended, down)

    @staticmethod
    def unicode(unit: int, down: bool) -> KeyAction:
        return KeyAction("unicode", unit, False, down)


def _split(notation: str) -> tuple[list[str], KeyRef | None]:
    """Splits a+b+key into lowercased modifier names and the key; the key is None when the last token
    is a modifier name."""
    tokens = notation.split("+")
    last = tokens[-1]
    if not last or any(not token for token in tokens[:-1]):
        raise ChordNotationError(notation, "empty part (write '+' as 'plus')")
    names = [token.lower() for token in tokens[:-1]]
    lowered = last.lower()
    if lowered in _MODIFIER_NAMES:
        return names + [lowered], None
    if last == "*":
        raise ChordNotationError(notation, "'*' is only allowed in reserved shortcuts")
    if lowered == "plus":
        return names, KeyRef("character", "+")
    named = lowered if lowered in NAMED_KEYS else _NAMED_ALIASES.get(lowered)
    if named is not None:
        return names, KeyRef("named", named)
    if len(last) == 1:
        return names, KeyRef("character", lowered)
    raise ChordNotationError(notation, f"unknown key '{last}'")


def parse_chord(notation: str) -> WindowsChord:
    """Parses ctrl+alt+shift+win+<key>; the key is a single character, a NAMED_KEYS name, or "plus".
    A chord of modifiers only ("win") taps them."""
    names, key = _split(notation)
    modifiers = WindowsModifier(0)
    for name in names:
        flag = _WINDOWS_MODIFIER_NAMES.get(name)
        if flag is None:
            raise ChordNotationError(notation, f"unknown Windows modifier '{name}'")
        modifiers |= flag
    if key is None and not modifiers:
        raise ChordNotationError(notation, "empty chord")
    return WindowsChord(modifiers, key)


def key_target(key: KeyRef, layout_id: int) -> tuple[int, bool] | str | None:
    """(scancode, extended), PAUSE, or None when the layout has no key for the character."""
    if key.kind == "named":
        return NAMED_KEYS[key.value]
    scancode = windows_layouts.base_key(layout_id, key.value)
    return None if scancode is None else (scancode, False)


def tap_actions(chord: WindowsChord, layout_id: int) -> list[KeyAction]:
    """Presses the modifiers, taps the key, releases the modifiers in reverse. Empty when a character
    has no key on the layout (the caller reports it)."""
    target = None
    if chord.key is not None:
        target = key_target(chord.key, layout_id)
        if target is None:
            return []
    held = [(code, extended) for flag, code, extended in _MODIFIER_KEYS if chord.modifiers & flag]
    actions = [KeyAction.scancode(code, extended, True) for code, extended in held]
    if target == PAUSE:
        actions.append(KeyAction("pause"))
    elif target is not None:
        code, extended = target
        actions += [KeyAction.scancode(code, extended, True), KeyAction.scancode(code, extended, False)]
    actions += [KeyAction.scancode(code, extended, False) for code, extended in reversed(held)]
    return actions


class KeySink(Protocol):
    """What send_actions needs of weitblick.rdp.session.RDPSession."""

    def send_scancode(self, code: int, extended: bool, down: bool) -> None: ...
    def send_pause(self) -> None: ...
    def send_unicode_unit(self, code_unit: int, down: bool) -> None: ...


def send_actions(session: KeySink, actions: list[KeyAction]) -> None:
    for action in actions:
        if action.kind == "scancode":
            session.send_scancode(action.code, action.extended, action.down)
        elif action.kind == "unicode":
            session.send_unicode_unit(action.code, action.down)
        elif action.kind == "pause":
            session.send_pause()
