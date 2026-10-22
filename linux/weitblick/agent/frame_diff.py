"""Telling when the screen has settled. Port of FrameDiff.swift: frames are BGRA bytes (4 bytes per
pixel, rows `stride` bytes apart) as weitblick.rdp.session.Frame carries them. A blinking caret is
not a change worth waiting for."""

from __future__ import annotations

from .screen_mapping import DesktopRect


def changed_bounds(old: bytes, new: bytes, width: int, height: int, stride: int) -> DesktopRect | None:
    """The bounding box of all changed pixels, or None when the frames are equal (or don't fit)."""
    row_bytes = width * 4
    needed = stride * (height - 1) + row_bytes
    if width <= 0 or height <= 0 or stride < row_bytes or len(old) < needed or len(new) < needed:
        return None
    old_view, new_view = memoryview(old), memoryview(new)
    min_x, min_y, max_x, max_y = width, height, -1, -1
    for y in range(height):
        start = y * stride
        old_row = old_view[start:start + row_bytes]
        new_row = new_view[start:start + row_bytes]
        if old_row == new_row:
            continue
        min_y = min(min_y, y)
        max_y = y
        # XOR of the rows as one integer: its lowest and highest set bits are the first and last
        # changed bytes (little endian), found at C speed.
        diff = int.from_bytes(old_row, "little") ^ int.from_bytes(new_row, "little")
        first = ((diff & -diff).bit_length() - 1) // 8 // 4
        last = (diff.bit_length() - 1) // 8 // 4
        min_x = min(min_x, first)
        max_x = max(max_x, last)
    if max_y < 0:
        return None
    return DesktopRect(min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)


def is_significant(bounds: DesktopRect | None, max_ignored_width: int = 4, max_ignored_height: int = 48) -> bool:
    """Whether a change is more than a caret: wider than `max_ignored_width` or taller than
    `max_ignored_height`."""
    if bounds is None:
        return False
    return bounds.width > max_ignored_width or bounds.height > max_ignored_height


class SettleTracker:
    """Decides when a polled screen has settled: no significant change for `settle` seconds."""

    def __init__(self, settle: float, start: float):
        self.settle = settle
        self.last_change = start

    def update(self, changed: bool, at: float) -> bool:
        """Feed one poll; True once nothing significant changed for `settle` seconds."""
        if changed:
            self.last_change = at
        return at - self.last_change >= self.settle
