"""The session's Windows keyboard layout (KLID), derived from the local XKB layout.

The remote layout should match the local one: keys are sent as scancodes, so Windows turns them into
characters with its own layout. A connection can override the layout (KeyboardConfig.layoutOverride).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from ..i18n import _

log = logging.getLogger(__name__)

US_ENGLISH = 0x0409
GERMAN = 0x0407


@dataclass(frozen=True)
class WindowsLayout:
    klid: int
    name: str


def known_layouts() -> list[WindowsLayout]:
    """Layouts offered in the connection editor, by name."""
    layouts = [
        WindowsLayout(0x0407, _("German")),
        WindowsLayout(0x0807, _("German (Switzerland)")),
        WindowsLayout(0x0409, _("English (US)")),
        WindowsLayout(0x20409, _("English (US, international)")),
        WindowsLayout(0x0809, _("English (UK)")),
        WindowsLayout(0x1809, _("English (Ireland)")),
        WindowsLayout(0x040C, _("French")),
        WindowsLayout(0x080C, _("French (Belgium)")),
        WindowsLayout(0x100C, _("French (Switzerland)")),
        WindowsLayout(0x1009, _("French (Canada)")),
        WindowsLayout(0x0413, _("Dutch")),
        WindowsLayout(0x0813, _("Dutch (Belgium)")),
        WindowsLayout(0x0410, _("Italian")),
        WindowsLayout(0x040A, _("Spanish")),
        WindowsLayout(0x080A, _("Spanish (Latin America)")),
        WindowsLayout(0x0816, _("Portuguese")),
        WindowsLayout(0x0416, _("Portuguese (Brazil)")),
        WindowsLayout(0x0406, _("Danish")),
        WindowsLayout(0x0414, _("Norwegian")),
        WindowsLayout(0x041D, _("Swedish")),
        WindowsLayout(0x040B, _("Finnish")),
        WindowsLayout(0x040F, _("Icelandic")),
        WindowsLayout(0x0415, _("Polish")),
        WindowsLayout(0x0405, _("Czech")),
        WindowsLayout(0x041B, _("Slovak")),
        WindowsLayout(0x040E, _("Hungarian")),
        WindowsLayout(0x0424, _("Slovenian")),
        WindowsLayout(0x041A, _("Croatian")),
        WindowsLayout(0x0418, _("Romanian")),
        WindowsLayout(0x0425, _("Estonian")),
        WindowsLayout(0x0426, _("Latvian")),
        WindowsLayout(0x0427, _("Lithuanian")),
        WindowsLayout(0x041F, _("Turkish")),
        WindowsLayout(0x0408, _("Greek")),
        WindowsLayout(0x0419, _("Russian")),
        WindowsLayout(0x0422, _("Ukrainian")),
        WindowsLayout(0x0402, _("Bulgarian")),
        WindowsLayout(0x0C1A, _("Serbian (Cyrillic)")),
        WindowsLayout(0x040D, _("Hebrew")),
        WindowsLayout(0x0401, _("Arabic")),
        WindowsLayout(0x0411, _("Japanese")),
        WindowsLayout(0x0412, _("Korean")),
        WindowsLayout(0x0804, _("Chinese (Simplified)")),
        WindowsLayout(0x0404, _("Chinese (Traditional)")),
    ]
    return sorted(layouts, key=lambda layout: layout.name.casefold())


def layout_name(klid: int) -> str:
    for layout in known_layouts():
        if layout.klid == klid:
            return layout.name
    return f"0x{klid:08X}"


# XKB layout (and layout(variant)) -> Windows KLID. Variants only where Windows has its own layout.
_XKB_TO_KLID: dict[str, int] = {
    "us": 0x0409, "us(intl)": 0x20409, "us(alt-intl)": 0x20409, "us(dvorak)": 0x10409,
    "gb": 0x0809, "ie": 0x1809,
    "de": 0x0407, "at": 0x0407,
    "ch": 0x0807, "ch(de)": 0x0807, "ch(fr)": 0x100C, "ch(fr_nodeadkeys)": 0x100C, "ch(fr_mac)": 0x100C,
    "fr": 0x040C, "be": 0x080C, "ca": 0x1009, "ca(fr)": 0x1009, "ca(eng)": 0x0409,
    "nl": 0x0413, "it": 0x0410, "es": 0x040A, "latam": 0x080A, "pt": 0x0816, "br": 0x0416,
    "dk": 0x0406, "no": 0x0414, "se": 0x041D, "fi": 0x040B, "is": 0x040F,
    "pl": 0x0415, "cz": 0x0405, "sk": 0x041B, "hu": 0x040E, "si": 0x0424, "hr": 0x041A, "ro": 0x0418,
    "ee": 0x0425, "lv": 0x0426, "lt": 0x0427, "tr": 0x041F, "gr": 0x0408,
    "ru": 0x0419, "ua": 0x0422, "by": 0x0423, "bg": 0x0402, "rs": 0x0C1A,
    "il": 0x040D, "ara": 0x0401, "jp": 0x0411, "kr": 0x0412, "cn": 0x0804, "tw": 0x0404,
}


def klid_for_xkb(layout: str, variant: str = "") -> int | None:
    """The Windows layout for an XKB layout name, e.g. ("de", "nodeadkeys") -> 0x0407."""
    layout = layout.strip().lower()
    variant = variant.strip().lower()
    if variant and f"{layout}({variant})" in _XKB_TO_KLID:
        return _XKB_TO_KLID[f"{layout}({variant})"]
    return _XKB_TO_KLID.get(layout)


def parse_input_source(source: str) -> tuple[str, str]:
    """GNOME input source ids look like "de+nodeadkeys"; returns (layout, variant)."""
    layout, _sep, variant = source.partition("+")
    return layout, variant


def first_xkb_source(sources) -> tuple[str, str] | None:
    """(layout, variant) of the first XKB entry in a list of GNOME input sources [(kind, id), …]."""
    for kind, name in sources:
        if kind == "xkb":
            return parse_input_source(name)
    return None


# mru-sources lists the most recently used (i.e. current) source first.
_INPUT_SOURCE_KEYS = ("mru-sources", "sources")


def _gnome_input_source() -> tuple[str, str] | None:
    try:
        from gi.repository import Gio
        source = Gio.SettingsSchemaSource.get_default()
        if source is None or source.lookup("org.gnome.desktop.input-sources", True) is None:
            return None
        settings = Gio.Settings.new("org.gnome.desktop.input-sources")
        for key in _INPUT_SOURCE_KEYS:
            found = first_xkb_source(settings.get_value(key).unpack())
            if found:
                return found
    except Exception:  # noqa: BLE001 - detection is best effort
        log.debug("GNOME input sources not readable", exc_info=True)
    return None


def _portal_input_source() -> tuple[str, str] | None:
    """GNOME's input sources through the settings portal: in the Flatpak, GSettings are the sandbox's own."""
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        for key in _INPUT_SOURCE_KEYS:
            reply = bus.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                                  "org.freedesktop.portal.Settings", "Read",
                                  GLib.Variant("(ss)", ("org.gnome.desktop.input-sources", key)),
                                  GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 1000, None)
            found = first_xkb_source(reply.unpack()[0])  # unpack() unwraps Read's nested variants
            if found:
                return found
    except Exception:  # noqa: BLE001 - no portal, or a desktop without GNOME's settings
        log.debug("Input sources not readable through the settings portal", exc_info=True)
    return None


def _setxkbmap() -> tuple[str, str] | None:
    try:
        output = subprocess.run(["setxkbmap", "-query"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    values = dict(line.split(":", 1) for line in output.splitlines() if ":" in line)
    layouts = values.get("layout", "").strip().split(",")
    variants = values.get("variant", "").strip().split(",")
    if not layouts or not layouts[0]:
        return None
    return layouts[0], variants[0] if variants else ""


def current_klid() -> int:
    """The Windows layout matching the active local layout; US English if unknown."""
    for detect in (_gnome_input_source, _portal_input_source, _setxkbmap):
        found = detect()
        if found:
            klid = klid_for_xkb(*found)
            if klid is not None:
                return klid
            log.info("No Windows layout known for XKB layout %s(%s)", *found)
    return US_ENGLISH
