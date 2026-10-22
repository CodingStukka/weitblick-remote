"""Translations (gettext). The source language is English; po/de.po carries German.

    from weitblick.i18n import _
"""

import gettext

from . import paths

DOMAIN = "weitblick-remote"

_localedir = paths.locale_dir()
_translation = gettext.translation(DOMAIN, localedir=str(_localedir) if _localedir else None, fallback=True)

_ = _translation.gettext
ngettext = _translation.ngettext


def N_(message: str) -> str:
    """Marks a string for extraction without translating it here."""
    return message
