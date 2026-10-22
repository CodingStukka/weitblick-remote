"""Text as RDP keyboard input (port of TextTyping.swift): Unicode events, independent of the keyboard
layout, except line breaks and tabs, which press Enter and Tab so dialogs and editors react to them
like to keys. Plus the modifier keys held around a click."""

from __future__ import annotations

from ..keyboard.chords import KeyAction
from .errors import ToolArgumentError

_ENTER = (0x1C, False)
_TAB = (0x0F, False)


def _tap(key: tuple[int, bool]) -> list[KeyAction]:
    code, extended = key
    return [KeyAction.scancode(code, extended, True), KeyAction.scancode(code, extended, False)]


def typing_actions(text: str) -> list[list[KeyAction]]:
    """Actions grouped per character, so the caller can pace long texts between groups."""
    groups: list[list[KeyAction]] = []
    previous_was_carriage_return = False
    for char in text:
        if char == "\r":
            groups.append(_tap(_ENTER))
            previous_was_carriage_return = True
            continue
        if char == "\n":
            # "\r\n" is one line break.
            if not previous_was_carriage_return:
                groups.append(_tap(_ENTER))
        elif char == "\t":
            groups.append(_tap(_TAB))
        elif ord(char) >= 0x20 and ord(char) != 0x7F:
            # Other control characters would type nothing useful.
            units = char.encode("utf-16-le")
            group = []
            for index in range(0, len(units), 2):
                unit = int.from_bytes(units[index:index + 2], "little")
                group += [KeyAction.unicode(unit, True), KeyAction.unicode(unit, False)]
            groups.append(group)
        previous_was_carriage_return = False
    return groups


class HeldModifiers:
    """Windows modifier keys held around a click (modifiers: ["ctrl", "shift"])."""

    NAMES = ["ctrl", "shift", "alt", "win"]
    _KEYS = {"ctrl": (0x1D, False), "control": (0x1D, False), "shift": (0x2A, False), "alt": (0x38, False),
             "win": (0x5B, True), "windows": (0x5B, True), "super": (0x5B, True), "meta": (0x5B, True)}

    @classmethod
    def parse(cls, requested: list[str]) -> list[tuple[int, bool]]:
        """(scancode, extended) per distinct modifier, in the requested order."""
        keys: list[tuple[int, bool]] = []
        for name in requested:
            key = cls._KEYS.get(name.lower())
            if key is None:
                raise ToolArgumentError(f"Unknown modifier '{name}'; use {', '.join(cls.NAMES)}.")
            if key not in keys:
                keys.append(key)
        return keys

    @staticmethod
    def press(keys: list[tuple[int, bool]]) -> list[KeyAction]:
        return [KeyAction.scancode(code, extended, True) for code, extended in keys]

    @staticmethod
    def release(keys: list[tuple[int, bool]]) -> list[KeyAction]:
        return [KeyAction.scancode(code, extended, False) for code, extended in reversed(keys)]
