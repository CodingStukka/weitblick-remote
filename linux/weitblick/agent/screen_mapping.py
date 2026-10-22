"""Between desktop pixels (what RDP uses) and screenshot pixels (what the model sees). Port of
ScreenMapping.swift: desktops whose long side exceeds MAX_SCREENSHOT_SIDE are scaled down for
screenshots; everything else is 1:1."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .errors import ToolArgumentError, format_number, round_half_away

#: Longest screenshot side; larger desktops are scaled down.
MAX_SCREENSHOT_SIDE = 1280


@dataclass(frozen=True)
class PixelPoint:
    """A point in screenshot pixels (as the model sees them)."""

    x: float
    y: float


@dataclass(frozen=True)
class PixelRect:
    x: float
    y: float
    width: float
    height: float

    @property
    def max_x(self) -> float:
        return self.x + self.width

    @property
    def max_y(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> PixelPoint:
        return PixelPoint(self.x + self.width / 2, self.y + self.height / 2)


@dataclass(frozen=True)
class DesktopRect:
    """A rectangle on the remote desktop in whole pixels."""

    x: int
    y: int
    width: int
    height: int


class ScreenMapping:
    def __init__(self, desktop_width: int, desktop_height: int, max_side: int = MAX_SCREENSHOT_SIDE):
        self.desktop_width = max(1, desktop_width)
        self.desktop_height = max(1, desktop_height)
        long_side = max(self.desktop_width, self.desktop_height)
        #: Screenshot pixels per desktop pixel, ≤ 1.
        self.scale = max_side / long_side if long_side > max_side else 1.0

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, ScreenMapping) and other.desktop_width == self.desktop_width
                and other.desktop_height == self.desktop_height and other.scale == self.scale)

    @property
    def screenshot_width(self) -> int:
        return max(1, int(round_half_away(self.desktop_width * self.scale)))

    @property
    def screenshot_height(self) -> int:
        return max(1, int(round_half_away(self.desktop_height * self.scale)))

    def desktop_point(self, point: PixelPoint) -> tuple[int, int]:
        """The desktop pixel under a screenshot point; raises outside the screenshot."""
        if not (0 <= point.x < self.screenshot_width + 0.5 and 0 <= point.y < self.screenshot_height + 0.5):
            raise ToolArgumentError(
                f"Point {format_number(point.x)},{format_number(point.y)} is outside the screen "
                f"(0…{self.screenshot_width - 1}, 0…{self.screenshot_height - 1} in screenshot pixels).")
        x = math.floor(point.x / self.scale)
        y = math.floor(point.y / self.scale)
        return min(x, self.desktop_width - 1), min(y, self.desktop_height - 1)

    def desktop_rect(self, region: PixelRect) -> DesktopRect:
        """The desktop pixels covering a screenshot region, clipped to the screen."""
        min_x = max(0, math.floor(region.x / self.scale))
        min_y = max(0, math.floor(region.y / self.scale))
        max_x = min(self.desktop_width, math.ceil(region.max_x / self.scale))
        max_y = min(self.desktop_height, math.ceil(region.max_y / self.scale))
        if max_x <= min_x or max_y <= min_y:
            raise ToolArgumentError(
                f"The region is outside the screen ({self.screenshot_width}×{self.screenshot_height} "
                "screenshot pixels).")
        return DesktopRect(min_x, min_y, max_x - min_x, max_y - min_y)

    def screenshot_rect(self, rect: PixelRect) -> PixelRect:
        """A desktop rectangle in screenshot pixels."""
        s = self.scale
        return PixelRect(rect.x * s, rect.y * s, rect.width * s, rect.height * s)

    def screenshot_point(self, desktop_x: float, desktop_y: float) -> PixelPoint:
        return PixelPoint(desktop_x * self.scale, desktop_y * self.scale)

    @property
    def summary(self) -> str:
        """For tool results: the screenshot size and how it relates to the desktop."""
        if self.scale == 1:
            return (f"{self.screenshot_width}×{self.screenshot_height} px, scale 1 "
                    "(screenshot pixels are desktop pixels)")
        return (f"{self.screenshot_width}×{self.screenshot_height} px, scale {format_number(self.scale, 4)} "
                f"(desktop {self.desktop_width}×{self.desktop_height}; coordinates are mapped for you)")
