"""The error tool arguments raise, and number formatting shared by the agent modules."""

from __future__ import annotations

import math


class ToolArgumentError(Exception):
    """A wrong tool argument; the message goes back to the model as an error result."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ToolArgumentError) and other.message == self.message

    def __hash__(self) -> int:
        return hash(self.message)


def format_number(value: float, digits: int = 1) -> str:
    """Whole numbers without a fraction ("640"), others rounded to `digits` places ("0.6667")."""
    if value == round_half_away(value) and abs(value) < 1e9:
        return str(int(value))
    return str(round(value, digits))


def round_half_away(value: float) -> float:
    """Swift's rounded(): halves away from zero (Python's round() rounds halves to even)."""
    return math.copysign(math.floor(abs(value) + 0.5), value)
