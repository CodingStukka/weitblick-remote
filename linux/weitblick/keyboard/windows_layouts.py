"""Static Windows keyboard layouts: which set-1 key types which character.

Agents (and the session menu) press keys by name, and "ctrl+z" means the key that types "z" in the
session's Windows layout: on German that is the physical Y key (0x15). The app cannot ask Windows,
so the main block of the common layouts is spelled out here, three levels per key (unshifted,
Shift, AltGr). Unknown layouts fall back to US, like Windows shortcuts on non-Latin layouts.
"""

from __future__ import annotations

from dataclasses import dataclass

US_ENGLISH = 0x0409
GERMAN = 0x0407
SWISS_GERMAN = 0x0807
AUSTRIAN = 0x0C07
UK_ENGLISH = 0x0809
FRENCH = 0x040C
SPANISH = 0x040A
ITALIAN = 0x0410

# The main block row by row, as set-1 scancodes (ISO keyboard: 0x56 is the extra key left of Z).
_ROW_DIGITS = [0x29, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D]
_ROW_Q = [0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B]
_ROW_A = [0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2B]
_ROW_Z = [0x56, 0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x32, 0x33, 0x34, 0x35]
_ROWS = [_ROW_DIGITS, _ROW_Q, _ROW_A, _ROW_Z]

SPACE = 0x39


@dataclass(frozen=True)
class KeyPosition:
    """Where a character is: the key plus the modifiers that select its level."""

    scancode: int
    shift: bool = False
    altgr: bool = False
    #: A dead key: it types nothing until the next key (Windows composes the accent).
    dead: bool = False


@dataclass(frozen=True)
class _Layout:
    # scancode -> (unshifted, Shift, AltGr); "" = nothing on that level.
    keys: dict[int, tuple[str, str, str]]
    dead: frozenset[str]


def _layout(rows: list[tuple[str, str, str]], dead: str = "") -> _Layout:
    """Each row is three strings (unshifted, Shift, AltGr), one character per key; a space means
    the level is empty (no key in these rows types a space)."""
    keys: dict[int, tuple[str, str, str]] = {}
    for scancodes, levels in zip(_ROWS, rows):
        for level in levels:
            if len(level) != len(scancodes):
                raise ValueError(f"row {levels!r} does not match {len(scancodes)} keys")
        for index, scancode in enumerate(scancodes):
            keys[scancode] = tuple("" if level[index] == " " else level[index] for level in levels)
    return _Layout(keys, frozenset(dead))


_EMPTY_13 = " " * 13
_EMPTY_12 = " " * 12
_EMPTY_11 = " " * 11

_US = _layout([
    ("`1234567890-=", "~!@#$%^&*()_+", _EMPTY_13),
    ("qwertyuiop[]", "QWERTYUIOP{}", _EMPTY_12),
    ("asdfghjkl;'\\", "ASDFGHJKL:\"|", _EMPTY_12),
    ("\\zxcvbnm,./", "|ZXCVBNM<>?", _EMPTY_11),
])

_GERMAN = _layout([
    ("^1234567890ß´", "°!\"§$%&/()=?`", "  ²³   {[]}\\ "),
    ("qwertzuiopü+", "QWERTZUIOPÜ*", "@ €        ~"),
    ("asdfghjklöä#", "ASDFGHJKLÖÄ'", _EMPTY_12),
    ("<yxcvbnm,.-", ">YXCVBNM;:_", "|      µ   "),
], dead="^´`")

_SWISS_GERMAN = _layout([
    ("§1234567890'^", "°+\"*ç%&/()=?`", " ¦@#°§¬|¢  ´~"),
    ("qwertzuiopü¨", "QWERTZUIOPè!", "  €       []"),
    ("asdfghjklöä$", "ASDFGHJKLéà£", "          {}"),
    ("<yxcvbnm,.-", ">YXCVBNM;:_", "\\          "),
], dead="^`¨´~")

_UK = _layout([
    ("`1234567890-=", "¬!\"£$%^&*()_+", "¦   €        "),
    ("qwertyuiop[]", "QWERTYUIOP{}", "  é   úíó   "),
    ("asdfghjkl;'#", "ASDFGHJKL:@~", "á           "),
    ("\\zxcvbnm,./", "|ZXCVBNM<>?", _EMPTY_11),
])

_FRENCH = _layout([
    ("²&é\"'(-è_çà)=", " 1234567890°+", "  ~#{[|`\\^@]}"),
    ("azertyuiop^$", "AZERTYUIOP¨£", "  €        ¤"),
    ("qsdfghjklmù*", "QSDFGHJKLM%µ", _EMPTY_12),
    ("<wxcvbn,;:!", ">WXCVBN?./§", _EMPTY_11),
], dead="^¨~`")

_SPANISH = _layout([
    ("º1234567890'¡", "ª!\"·$%&/()=?¿", "\\|@#~€¬      "),
    ("qwertyuiop`+", "QWERTYUIOP^*", "  €       []"),
    ("asdfghjklñ´ç", "ASDFGHJKLÑ¨Ç", "          {}"),
    ("<zxcvbnm,.-", ">ZXCVBNM;:_", _EMPTY_11),
], dead="`^´¨")

_ITALIAN = _layout([
    ("\\1234567890'ì", "|!\"£$%&/()=?^", _EMPTY_13),
    ("qwertyuiopè+", "QWERTYUIOPé*", "  €       []"),
    ("asdfghjklòàù", "ASDFGHJKLç°§", "         @# "),
    ("<zxcvbnm,.-", ">ZXCVBNM;:_", _EMPTY_11),
])

_LAYOUTS: dict[int, _Layout] = {
    US_ENGLISH: _US,
    GERMAN: _GERMAN,
    AUSTRIAN: _GERMAN,
    SWISS_GERMAN: _SWISS_GERMAN,
    UK_ENGLISH: _UK,
    FRENCH: _FRENCH,
    SPANISH: _SPANISH,
    ITALIAN: _ITALIAN,
}

#: US positions of the characters shortcuts use: the fallback for characters a layout lacks
#: (Latin letters on Cyrillic layouts), like Windows itself resolves shortcuts.
_US_FALLBACK = {levels[0]: scancode for scancode, levels in _US.keys.items()
                if levels[0] and (levels[0].isalnum() or levels[0] in "=-][';\\,/.`") and scancode != 0x56}


def _table(layout_id: int) -> _Layout:
    """Exact KLID first (0x00000407), then its language (0x0407 for 0x00010407), else US."""
    return _LAYOUTS.get(layout_id) or _LAYOUTS.get(layout_id & 0xFFFF) or _US


def has_table(layout_id: int) -> bool:
    """Whether the layout has its own table (otherwise US positions are used)."""
    return layout_id in _LAYOUTS or (layout_id & 0xFFFF) in _LAYOUTS


def base_key(layout_id: int, character: str) -> int | None:
    """The main-block key that types `character` unmodified (case-insensitive), for shortcuts:
    "z" on German is 0x15. Characters the layout lacks resolve to their US position; characters that
    need Shift or AltGr everywhere (such as "@") have no key."""
    if len(character) != 1:
        return None
    if character == " ":
        return SPACE
    wanted = character.lower()
    for scancodes in _ROWS:
        for scancode in scancodes:
            base = _table(layout_id).keys[scancode][0]
            if base and base.lower() == wanted:
                return scancode
    return _US_FALLBACK.get(wanted)


def key_for_character(layout_id: int, character: str) -> KeyPosition | None:
    """Where `character` is on the layout at any level, preferring the unshifted, then Shift, then
    AltGr level; None if the layout has no key for it."""
    if len(character) != 1:
        return None
    if character == " ":
        return KeyPosition(SPACE)
    layout = _table(layout_id)
    for level in range(3):
        for scancodes in _ROWS:
            for scancode in scancodes:
                if layout.keys[scancode][level] == character:
                    return KeyPosition(scancode, shift=level == 1, altgr=level == 2,
                                       dead=character in layout.dead)
    return None


def characters(layout_id: int, scancode: int) -> tuple[str, str, str] | None:
    """The (unshifted, Shift, AltGr) characters of a main-block key, "" for empty levels."""
    if scancode == SPACE:
        return (" ", " ", "")
    return _table(layout_id).keys.get(scancode)
