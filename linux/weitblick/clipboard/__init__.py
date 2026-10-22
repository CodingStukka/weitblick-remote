"""Clipboard sync between the desktop clipboard and a session's RDP clipboard channel (cliprdr).

    sync = ClipboardSync(session.clipboard, display.get_clipboard())   # None: isolated agent clipboard

Formats: text (CF_UNICODETEXT), HTML ("HTML Format"), RTF, images ("PNG", CF_DIBV5, CF_DIB) and files
(FileGroupDescriptorW + FileContents), in both directions.
"""

from .sync import ClipboardSync

__all__ = ["ClipboardSync"]
