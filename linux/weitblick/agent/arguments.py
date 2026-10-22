"""Typed access to a tools/call arguments object with errors the model can act on. Port of
ToolArguments.swift. Unknown keys are ignored."""

from __future__ import annotations

import math
from typing import Any

from .errors import ToolArgumentError, round_half_away
from .screen_mapping import PixelPoint, PixelRect

__all__ = ["ToolArgumentError", "ToolArguments"]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class ToolArguments:
    def __init__(self, value: Any):
        self.raw: dict[str, Any] = value if isinstance(value, dict) else {}

    def has(self, key: str) -> bool:
        return self.raw.get(key) is not None

    def string(self, key: str) -> str:
        value = self.optional_string(key)
        if value is None:
            raise ToolArgumentError(f"Missing argument '{key}'.")
        return value

    def optional_string(self, key: str) -> str | None:
        if not self.has(key):
            return None
        value = self.raw[key]
        if not isinstance(value, str):
            raise ToolArgumentError(f"'{key}' must be a string.")
        return value

    def non_empty_string(self, key: str) -> str:
        """A string that must not be empty after trimming."""
        value = self.string(key)
        if not value.strip():
            raise ToolArgumentError(f"'{key}' is empty.")
        return value

    def optional_bool(self, key: str) -> bool | None:
        if not self.has(key):
            return None
        value = self.raw[key]
        if isinstance(value, bool):
            return value
        # Some clients send flags as strings.
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ToolArgumentError(f"'{key}' must be true or false.")

    def bool(self, key: str, default: bool) -> bool:
        value = self.optional_bool(key)
        return default if value is None else value

    def optional_number(self, key: str) -> float | None:
        if not self.has(key):
            return None
        value = self.raw[key]
        if _is_number(value) and math.isfinite(value):
            return float(value)
        if isinstance(value, str) and value == value.strip():
            try:
                number = float(value)
            except ValueError:
                number = math.nan
            if math.isfinite(number):
                return number
        raise ToolArgumentError(f"'{key}' must be a number.")

    def number(self, key: str, minimum: float | None = None, maximum: float | None = None,
               default: float | None = None) -> float:
        """A number, optionally within minimum…maximum; fractions are rounded by integer()."""
        value = self.optional_number(key)
        if value is None:
            value = default
        if value is None:
            raise ToolArgumentError(f"Missing argument '{key}'.")
        if minimum is not None and maximum is not None and not minimum <= value <= maximum:
            raise ToolArgumentError(
                f"'{key}' must be between {self._format(minimum)} and {self._format(maximum)}.")
        return value

    def integer(self, key: str, minimum: int, maximum: int, default: int | None = None) -> int:
        number = self.optional_number(key)
        value = int(round_half_away(number)) if number is not None else default
        if value is None:
            raise ToolArgumentError(f"Missing argument '{key}'.")
        if not minimum <= value <= maximum:
            raise ToolArgumentError(f"'{key}' must be between {minimum} and {maximum}.")
        return value

    def choice(self, key: str, choices: list[str], default: str) -> str:
        """One of `choices` (case-insensitive), or `default` when missing."""
        value = self.optional_string(key)
        if value is None:
            return default
        value = value.lower()
        if value not in choices:
            raise ToolArgumentError(f"'{key}' must be one of: {', '.join(choices)}.")
        return value

    def string_array(self, key: str) -> list[str]:
        if not self.has(key):
            return []
        value = self.raw[key]
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ToolArgumentError(f"'{key}' must be a list of strings.")
        return list(value)

    def optional_rect(self, key: str) -> PixelRect | None:
        """{x, y, width, height} in screenshot pixels, or None when missing."""
        if not self.has(key):
            return None
        value = self.raw[key]
        if not isinstance(value, dict):
            raise ToolArgumentError(f"'{key}' must be an object with x, y, width and height.")
        numbers = []
        for name in ("x", "y", "width", "height"):
            item = value.get(name)
            if not _is_number(item) or not math.isfinite(item):
                raise ToolArgumentError(f"'{key}' needs a number '{name}'.")
            numbers.append(float(item))
        if numbers[2] < 1 or numbers[3] < 1:
            raise ToolArgumentError(f"'{key}' must be at least 1×1 pixels.")
        return PixelRect(*numbers)

    def points(self, key: str) -> list[PixelPoint]:
        """A list of points, as [[x, y], …] or [{"x": …, "y": …}, …]."""
        items = self.raw.get(key)
        error = ToolArgumentError(f"'{key}' must be a list of [x, y] points.")
        if not isinstance(items, list):
            raise error
        points = []
        for item in items:
            if isinstance(item, list) and len(item) == 2 and all(_is_number(v) for v in item):
                points.append(PixelPoint(float(item[0]), float(item[1])))
            elif isinstance(item, dict) and _is_number(item.get("x")) and _is_number(item.get("y")):
                points.append(PixelPoint(float(item["x"]), float(item["y"])))
            else:
                raise error
        return points

    @staticmethod
    def _format(value: float) -> str:
        return str(int(value)) if value == round_half_away(value) else str(value)
