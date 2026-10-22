"""The `key` tool's notation: Windows combos like ctrl+shift+esc, win+r, alt+f4, enter, several
separated by spaces. Port of KeyCombo.swift: accepts the names models commonly use (super, pgup,
arrowleft, ctrl++) and maps them onto weitblick.keyboard.chords, whose characters resolve through
the session's keyboard layout."""

from __future__ import annotations

from ..keyboard.chords import ChordNotationError, KeyAction, WindowsChord, parse_chord, tap_actions
from .catalog import KEY_NAME_SUMMARY
from .errors import ToolArgumentError

__all__ = ["KEY_NAME_SUMMARY", "parse", "actions"]

#: Names models use → chord notation.
_ALIASES = {
    # Modifiers
    "control": "ctrl", "ctl": "ctrl", "strg": "ctrl", "super": "win", "meta": "win", "windows": "win",
    "start": "win", "lwin": "win", "rwin": "win", "option": "alt", "opt": "alt", "altgr": "ctrl+alt",
    "lctrl": "ctrl", "rctrl": "ctrl", "lshift": "shift", "rshift": "shift", "lalt": "alt", "ralt": "alt",
    # Named keys
    "return": "return", "enter": "return", "escape": "escape", "esc": "escape",
    "spacebar": "space", " ": "space", "bksp": "backspace", "back": "backspace",
    "del": "delete", "ins": "insert",
    "pgup": "pageup", "page_up": "pageup", "pgdn": "pagedown", "pgdown": "pagedown", "page_down": "pagedown",
    "arrowup": "up", "arrow_up": "up", "uparrow": "up",
    "arrowdown": "down", "arrow_down": "down", "downarrow": "down",
    "arrowleft": "left", "arrow_left": "left", "leftarrow": "left",
    "arrowright": "right", "arrow_right": "right", "rightarrow": "right",
    "prtsc": "printscreen", "prtscn": "printscreen", "print": "printscreen", "print_screen": "printscreen",
    "snapshot": "printscreen", "contextmenu": "menu", "context_menu": "menu", "apps": "menu",
    "scroll_lock": "scrolllock", "num_lock": "numlock", "caps_lock": "capslock", "break": "pause",
    # Symbols by name
    "minus": "-", "hyphen": "-", "equal": "=", "equals": "=", "comma": ",", "period": ".", "dot": ".",
    "slash": "/", "backslash": "\\", "semicolon": ";", "quote": "'", "apostrophe": "'",
    "backtick": "`", "grave": "`", "bracketleft": "[", "bracketright": "]",
}


def parse(notation: str) -> list[WindowsChord]:
    """Parses one or more combos separated by whitespace."""
    combos = notation.split()
    if not combos:
        raise ToolArgumentError("'keys' is empty.")
    return [_parse_combo(combo) for combo in combos]


def actions(chords: list[WindowsChord], layout_id: int, repeat: int = 1) -> list[KeyAction]:
    """Key events for pressing `chords` `repeat` times on the session's Windows layout. Raises for keys
    the layout cannot press."""
    result: list[KeyAction] = []
    for _ in range(max(1, repeat)):
        for chord in chords:
            tapped = tap_actions(chord, layout_id)
            if not tapped and chord.key is not None:
                raise ToolArgumentError(
                    f"'{chord}' has no key on the session's keyboard layout. Use type_text for characters.")
            result += tapped
    return result


def _parse_combo(combo: str) -> WindowsChord:
    # "ctrl++" and a lone "+" mean the plus key.
    text = combo
    if text == "+":
        text = "plus"
    elif text.endswith("++"):
        text = text[:-1] + "plus"
    parts = text.split("+")
    if any(not part for part in parts):
        raise ToolArgumentError(f"Invalid key combination '{combo}': empty part.")
    for index, part in enumerate(parts):
        lowered = part.lower()
        if lowered in ("cmd", "command", "⌘"):
            raise ToolArgumentError(
                f"Invalid key combination '{combo}': Windows has no Cmd key. Use ctrl (copy is ctrl+c) or win.")
        if lowered in _ALIASES:
            parts[index] = _ALIASES[lowered]
        elif len(part) == 1:
            parts[index] = lowered
    try:
        return parse_chord("+".join(parts))
    except ChordNotationError as error:
        raise ToolArgumentError(
            f"Invalid key combination '{combo}': {error.reason}. Keys: {KEY_NAME_SUMMARY}.") from None
