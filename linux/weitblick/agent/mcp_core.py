"""The protocol side of one MCP connection (newline-delimited JSON-RPC 2.0, tools only), without any
I/O. Port of MCPServerCore.swift and JSONValue.swift: feed it a line, it says what to send back or
which tool to run. Tool calls run elsewhere (they may wait); their answers go out through
`response(id, result)`.

    core = MCPServerCore("weitblick-remote", VERSION, catalog.INSTRUCTIONS, catalog.TOOLS)
    action = core.handle_line(line)
    if isinstance(action, Reply): send(action.line)
    elif isinstance(action, CallTool): run it, then send(MCPServerCore.response(action.id, result))
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from .catalog import ToolDefinition

#: Protocol revisions this server speaks, newest first.
SUPPORTED_PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def encode_json(value: Any) -> str:
    """Compact JSON on one line (newlines inside strings are escaped), keys sorted, Unicode kept.
    Integers stay integers; non-finite numbers become 0."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError:
        return encode_json(_finite(value))


def _finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


@dataclass(frozen=True)
class MCPClientInfo:
    """The MCP client as it introduced itself in initialize (clientInfo)."""

    name: str
    version: str = ""

    @property
    def display_name(self) -> str:
        """"claude-code 2.1.3", for prompts and the audit log."""
        return f"{self.name} {self.version}" if self.version else self.name


UNKNOWN_CLIENT = MCPClientInfo("unknown client")


@dataclass(frozen=True)
class TextContent:
    text: str

    def to_json(self) -> dict:
        return {"type": "text", "text": self.text}


@dataclass(frozen=True)
class ImageContent:
    """Base64 data plus MIME type (image/png, image/jpeg)."""

    data: str
    mime_type: str

    def to_json(self) -> dict:
        return {"type": "image", "data": self.data, "mimeType": self.mime_type}


@dataclass
class ToolResult:
    """What a tool returns. Failures are results with is_error (the model reads them), not JSON-RPC errors."""

    content: list[TextContent | ImageContent] = field(default_factory=list)
    is_error: bool = False

    @staticmethod
    def text(text: str) -> ToolResult:
        return ToolResult([TextContent(text)])

    @staticmethod
    def error(message: str) -> ToolResult:
        return ToolResult([TextContent(message)], is_error=True)

    def to_json(self) -> dict:
        result: dict[str, Any] = {"content": [block.to_json() for block in self.content]}
        if self.is_error:
            result["isError"] = True
        return result


@dataclass(frozen=True)
class Reply:
    """Send this line (a response or an error)."""

    line: str


@dataclass(frozen=True)
class CallTool:
    """Run the tool and send MCPServerCore.response(id, result) when done."""

    id: Any
    name: str
    arguments: dict


def negotiate(requested: str | None) -> str:
    """The client's version if we speak it, else our newest (the client then decides)."""
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return SUPPORTED_PROTOCOL_VERSIONS[0]


class MCPServerCore:
    def __init__(self, server_name: str, server_version: str, instructions: str, tools: list[ToolDefinition]):
        self.server_name = server_name
        self.server_version = server_version
        self.instructions = instructions
        self.tools = tools
        self.client = UNKNOWN_CLIENT
        self.protocol_version: str | None = None
        self.is_initialized = False

    def handle_line(self, line: str) -> Reply | CallTool | None:
        """None: nothing to send (notifications, responses from the client)."""
        try:
            message = json.loads(line)
        except ValueError:
            return Reply(self.error(None, PARSE_ERROR, "Parse error"))
        if not isinstance(message, dict):
            # Batches were removed from MCP in 2025-06-18; older clients never send them for tools.
            return Reply(self.error(None, INVALID_REQUEST, "Invalid request"))
        has_id = "id" in message
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            # A response from the client (we never ask it anything) or garbage.
            if has_id and ("result" in message or "error" in message):
                return None
            return Reply(self.error(request_id, INVALID_REQUEST, "Invalid request"))
        params = message.get("params", {})
        if request_id is None:
            return self._handle_notification(method)

        if method == "initialize":
            return Reply(self.result(request_id, self._initialize(params)))
        if method == "ping":
            return Reply(self.result(request_id, {}))
        if method == "tools/list":
            return Reply(self.result(request_id, {"tools": [tool.to_json() for tool in self.tools]}))
        if method == "tools/call":
            name = params.get("name") if isinstance(params, dict) else None
            if not isinstance(name, str):
                return Reply(self.error(request_id, INVALID_PARAMS, "tools/call needs a tool name"))
            if not any(tool.name == name for tool in self.tools):
                return Reply(self.error(request_id, INVALID_PARAMS, f"Unknown tool: {name}"))
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                return Reply(self.error(request_id, INVALID_PARAMS, "Tool arguments must be an object"))
            return CallTool(request_id, name, arguments)
        return Reply(self.error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}"))

    def _handle_notification(self, method: str) -> None:
        if method == "notifications/initialized":
            self.is_initialized = True
        # notifications/cancelled and others: nothing to answer.
        return None

    def _initialize(self, params: Any) -> dict:
        params = params if isinstance(params, dict) else {}
        requested = params.get("protocolVersion")
        version = negotiate(requested if isinstance(requested, str) else None)
        self.protocol_version = version
        info = params.get("clientInfo")
        if isinstance(info, dict) and isinstance(info.get("name"), str) and info["name"]:
            client_version = info.get("version")
            self.client = MCPClientInfo(info["name"], client_version if isinstance(client_version, str) else "")
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.server_name, "version": self.server_version},
            "instructions": self.instructions,
        }

    @staticmethod
    def response(request_id: Any, result: ToolResult) -> str:
        """The answer line for a finished tool call."""
        return MCPServerCore.result(request_id, result.to_json())

    @staticmethod
    def result(request_id: Any, result: Any) -> str:
        return encode_json({"jsonrpc": "2.0", "id": request_id, "result": result})

    @staticmethod
    def error(request_id: Any, code: int, message: str) -> str:
        return encode_json({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})
