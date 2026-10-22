"""The tools Weitblick Remote offers agents, their JSON schemas and the server instructions. Port of
ToolCatalog.swift; tool names and schemas are identical to the macOS app."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .screen_mapping import MAX_SCREENSHOT_SIDE

__all__ = ["ToolDefinition", "TOOLS", "INSTRUCTIONS", "MAX_SCREENSHOT_SIDE", "definition"]

KEY_NAME_SUMMARY = ("ctrl, shift, alt, win; enter, esc, tab, space, backspace, delete, insert, home, end, "
                    "pageup, pagedown, up, down, left, right, f1-f20, printscreen, pause, menu, letters, digits "
                    "and symbols (plus, minus, comma, period, slash)")


@dataclass(frozen=True)
class ToolDefinition:
    """One MCP tool as tools/list describes it."""

    name: str
    title: str
    description: str
    #: JSON Schema of the arguments object.
    input_schema: dict
    #: Only looks (screenshots, OCR, lists); a hint for clients that auto-approve read-only tools.
    read_only: bool

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": self.read_only,
                "destructiveHint": not self.read_only,
                "openWorldHint": True,
            },
        }


INSTRUCTIONS = """\
Weitblick Remote controls Windows computers over RDP: screenshots, mouse, keyboard, clipboard \
and on-screen text. Nothing touches the user's computer.
- Start with list_connections, then connect. To work in a session the user already has open, \
use its id from list_sessions instead (connecting again would log the user out).
- Take a screenshot first and whenever you are unsure what is on screen.
- Coordinates are screenshot pixels; the screenshot result states the size and scale.
- Prefer find_text to locate labels and buttons: it returns click points.
- Use type_text for text and key for shortcuts (ctrl+s, win+r, alt+f4, enter).
- Input tools accept screenshot: true and then return a screenshot once the screen has settled.
- Disconnect when you are done."""


# MARK: - Schema helpers

def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def _string(description: str) -> dict:
    return {"type": "string", "description": description}


def _boolean(description: str) -> dict:
    return {"type": "boolean", "description": description}


def _integer(description: str, minimum: int, maximum: int) -> dict:
    return {"type": "integer", "description": description, "minimum": minimum, "maximum": maximum}


def _number(description: str, minimum: float, maximum: float) -> dict:
    return {"type": "number", "description": description, "minimum": minimum, "maximum": maximum}


def _coordinate(axis: str) -> dict:
    return {"type": "number", "description": f"{axis} in screenshot pixels"}


_SESSION = _string("Session id from connect or list_sessions")
_SCREENSHOT = _boolean("Return a screenshot once the screen has settled")
_BUTTON = {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button (default left)"}
_MODIFIERS = {"type": "array", "description": "Keys held during the click",
              "items": {"type": "string", "enum": ["ctrl", "shift", "alt", "win"]}}
_REGION = {
    "type": "object",
    "description": "Only this part of the screen, in screenshot pixels",
    "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                   "width": {"type": "number"}, "height": {"type": "number"}},
    "required": ["x", "y", "width", "height"],
    "additionalProperties": False,
}


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        "list_connections", "List connections",
        "Saved RDP connections with their agent access (ask, always, never) and open sessions.",
        _schema({}), read_only=True),
    ToolDefinition(
        "list_sessions", "List sessions",
        "Open sessions: the ones you connected and the ones the user has open (use their id to attach; the "
        "user may be asked for permission).",
        _schema({}), read_only=True),
    ToolDefinition(
        "connect", "Connect",
        "Opens a session to a saved connection and waits until the desktop is up. Returns the session id and "
        "desktop size. The user may be asked for permission (up to 2 minutes). The session runs without a "
        "window unless show is true.",
        _schema({
            "connection": _string("Connection name or id from list_connections"),
            "width": _integer("Desktop width in pixels (800 to 1920, default 1280)", 800, 1920),
            "height": _integer("Desktop height in pixels (600 to 1200, default 800)", 600, 1200),
            "show": _boolean("Open a tab the user can watch (without bringing the app to the front)"),
        }, ["connection"]), read_only=False),
    ToolDefinition(
        "disconnect", "Disconnect",
        "Closes a session you connected. For a session of the user it only ends your control.",
        _schema({"session": _SESSION}, ["session"]), read_only=False),
    ToolDefinition(
        "screenshot", "Screenshot",
        "The current screen (PNG, JPEG when large) plus its size, scale and the cursor position.",
        _schema({"session": _SESSION, "region": _REGION}, ["session"]), read_only=True),
    ToolDefinition(
        "click", "Click",
        "Clicks at a point (screenshot pixels).",
        _schema({
            "session": _SESSION, "x": _coordinate("x"), "y": _coordinate("y"), "button": _BUTTON,
            "count": _integer("1 = click, 2 = double click, 3 = triple click (default 1)", 1, 3),
            "modifiers": _MODIFIERS, "screenshot": _SCREENSHOT,
        }, ["session", "x", "y"]), read_only=False),
    ToolDefinition(
        "mouse_move", "Move the mouse",
        "Moves the pointer (screenshot pixels), e.g. to show a tooltip.",
        _schema({"session": _SESSION, "x": _coordinate("x"), "y": _coordinate("y"), "screenshot": _SCREENSHOT},
                ["session", "x", "y"]), read_only=False),
    ToolDefinition(
        "mouse_down", "Press a mouse button",
        "Presses a mouse button and keeps it down (at x, y if given). Release it with mouse_up.",
        _schema({"session": _SESSION, "x": _coordinate("x"), "y": _coordinate("y"), "button": _BUTTON,
                 "screenshot": _SCREENSHOT}, ["session"]), read_only=False),
    ToolDefinition(
        "mouse_up", "Release a mouse button",
        "Releases a mouse button (at x, y if given).",
        _schema({"session": _SESSION, "x": _coordinate("x"), "y": _coordinate("y"), "button": _BUTTON,
                 "screenshot": _SCREENSHOT}, ["session"]), read_only=False),
    ToolDefinition(
        "drag", "Drag",
        "Presses the button at the first point, moves along the path and releases at the last.",
        _schema({
            "session": _SESSION,
            "path": {
                "type": "array",
                "description": "Points [[x, y], ...] in screenshot pixels, at least two",
                "minItems": 2,
                "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
            },
            "button": _BUTTON, "screenshot": _SCREENSHOT,
        }, ["session", "path"]), read_only=False),
    ToolDefinition(
        "scroll", "Scroll",
        "Turns the mouse wheel at a point. dy > 0 scrolls down, dx > 0 scrolls right (notches).",
        _schema({
            "session": _SESSION, "x": _coordinate("x"), "y": _coordinate("y"),
            "dx": _integer("Horizontal notches, positive scrolls right", -50, 50),
            "dy": _integer("Vertical notches, positive scrolls down", -50, 50),
            "screenshot": _SCREENSHOT,
        }, ["session", "x", "y"]), read_only=False),
    ToolDefinition(
        "type_text", "Type text",
        "Types text as Unicode input, independent of the keyboard layout. \\n presses Enter, \\t presses Tab. "
        "Types about 40 characters per second, up to 5000; for long text use clipboard_set and key ctrl+v.",
        _schema({"session": _SESSION, "text": _string("The text to type"), "screenshot": _SCREENSHOT},
                ["session", "text"]), read_only=False),
    ToolDefinition(
        "key", "Press keys",
        "Presses a Windows key combination such as ctrl+shift+esc, win+r, alt+f4, enter, f5 or ctrl+z; several "
        "separated by spaces are pressed one after another. Letters and symbols go through the session's "
        f"keyboard layout. Keys: {KEY_NAME_SUMMARY}.",
        _schema({
            "session": _SESSION, "keys": _string("Key combination(s), e.g. ctrl+s or \"ctrl+a ctrl+c\""),
            "repeat": _integer("How often to press it (default 1)", 1, 100),
            "screenshot": _SCREENSHOT,
        }, ["session", "keys"]), read_only=False),
    ToolDefinition(
        "clipboard_get", "Read the clipboard",
        "The text on the remote clipboard.",
        _schema({"session": _SESSION}, ["session"]), read_only=True),
    ToolDefinition(
        "clipboard_set", "Set the clipboard",
        "Puts text on the remote clipboard (paste it with key ctrl+v).",
        _schema({"session": _SESSION, "text": _string("The text")}, ["session", "text"]), read_only=False),
    ToolDefinition(
        "wait", "Wait",
        "Waits a number of seconds.",
        _schema({"session": _SESSION, "seconds": _number("Seconds to wait (up to 30)", 0, 30)},
                ["session", "seconds"]), read_only=True),
    ToolDefinition(
        "wait_for_screen", "Wait for the screen",
        "Waits until the screen stops changing (a blinking caret does not count).",
        _schema({
            "session": _SESSION,
            "timeout": _number("Seconds to wait at most (default 10, up to 60)", 0, 60),
            "settle_ms": _integer("How long nothing may change (default 500 ms)", 100, 5000),
        }, ["session"]), read_only=True),
    ToolDefinition(
        "read_screen", "Read the screen",
        "Recognizes the text on screen (German and English) with boxes and click points in screenshot pixels.",
        _schema({"session": _SESSION, "region": _REGION}, ["session"]), read_only=True),
    ToolDefinition(
        "find_text", "Find text",
        "Finds text on screen (case-insensitive, tolerant of small recognition errors) and returns matches with "
        "boxes and click points in screenshot pixels, best first.",
        _schema({"session": _SESSION, "query": _string("The text to find"), "region": _REGION},
                ["session", "query"]), read_only=True),
]


def definition(name: str) -> ToolDefinition | None:
    return next((tool for tool in TOOLS if tool.name == name), None)
