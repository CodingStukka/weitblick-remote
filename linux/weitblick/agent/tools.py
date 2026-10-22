"""The MCP tools on top of the app's sessions (port of AgentTools.swift; schemas in catalog.py).

Every call runs as a coroutine on the main loop (loop.py); input waits between events with GLib
timeouts, encoding and text recognition run on worker threads. Calls on one session run one after
another. Messages are English: the model reads them, whatever language the app shows.
"""

from __future__ import annotations

import base64
import logging
import math
import time
from typing import TYPE_CHECKING

from ..bridge import MouseButton
from ..keyboard.chords import KeyAction, send_actions
from ..store.connection import ConnectionProtocol
from . import keycombo, screen_capture, text_matching
from .audit import AuditEntry
from .errors import ToolArgumentError, format_number, round_half_away
from .frame_diff import SettleTracker, changed_bounds, is_significant
from .loop import from_callback, run_in_thread, sleep
from .mcp_core import ImageContent, TextContent, ToolResult, encode_json
from .ocr import recognize_async, upscale_factor
from .screen_mapping import DesktopRect, PixelPoint, PixelRect, ScreenMapping
from .arguments import ToolArguments
from .text_typing import HeldModifiers, typing_actions

if TYPE_CHECKING:
    from .host import AgentClient, AgentHost, AgentSessionRecord

log = logging.getLogger(__name__)

#: Pauses between input events, at the pace of a fast typist: Windows apps read modifier state
#: asynchronously, and some mix up Unicode characters that arrive faster.
KEY_PACE = 0.02
CHORD_PACE = 0.05
CHARACTER_PACE = 0.025
CONNECT_TIMEOUT = 90.0
#: About two minutes of typing; longer texts go through the clipboard.
MAX_TYPED_CHARACTERS = 5000
#: After a copy (ctrl+c) the clipboard change arrives shortly after the keys.
CLIPBOARD_CHANGE_WAIT = 1.5


class ToolFailure(Exception):
    """A tool failed for a reason the model should read (not an argument problem)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ToolCall:
    """What a tool call touched, for the audit log."""

    def __init__(self) -> None:
        self.connection: str | None = None


# MARK: - Pure helpers (tested)

def describe_point(point: tuple[int, int], mapping: ScreenMapping) -> str:
    shown = mapping.screenshot_point(point[0], point[1])
    return f"({int(round_half_away(shown.x))}, {int(round_half_away(shown.y))})"


def describe_box(box: PixelRect) -> str:
    return (f"({int(round_half_away(box.x))}, {int(round_half_away(box.y))}, "
            f"{int(round_half_away(box.width))}×{int(round_half_away(box.height))})")


def click_point(box: PixelRect) -> str:
    center = box.center
    return f"click ({int(round_half_away(center.x))}, {int(round_half_away(center.y))})"


def screenshot_size(rect: DesktopRect, mapping: ScreenMapping) -> tuple[int, int]:
    """The pixel size a desktop region has in the screenshot."""
    return (max(1, int(round_half_away(rect.width * mapping.scale))),
            max(1, int(round_half_away(rect.height * mapping.scale))))


def drag_steps(start: tuple[int, int], end: tuple[int, int], step: float = 16) -> list[tuple[int, int]]:
    """Intermediate pointer positions from `start` to `end` (inclusive), about every `step` pixels."""
    distance = math.hypot(end[0] - start[0], end[1] - start[1])
    steps = max(1, int(distance / step))
    return [(start[0] + int(round_half_away((end[0] - start[0]) * index / steps)),
             start[1] + int(round_half_away((end[1] - start[1]) * index / steps))) for index in range(1, steps + 1)]


def wheel_events(dx: int, dy: int) -> list[tuple[int, int]]:
    """(vertical, horizontal) wheel events for the tool's notches. RDP's positive vertical scrolls up;
    the tool's dy > 0 scrolls down. Positive horizontal scrolls right in both."""
    events = [(-120 if dy > 0 else 120, 0)] * abs(dy)
    events += [(0, 120 if dx > 0 else -120)] * abs(dx)
    return events


def scroll_description(dx: int, dy: int) -> str:
    parts = []
    if dy:
        parts.append(f"{abs(dy)} notches {'down' if dy > 0 else 'up'}")
    if dx:
        parts.append(f"{abs(dx)} notches {'right' if dx > 0 else 'left'}")
    return " and ".join(parts)


def status_name(status) -> str:
    """English for the model, whatever language the app shows."""
    if status.kind == "reconnecting":
        return f"reconnecting (attempt {status.attempt})"
    return status.kind


def host_and_port(connection) -> str:
    return connection.host if connection.port == 3389 else f"{connection.host}:{connection.port}"


def label(connection) -> str:
    return f"{connection.name} ({connection.host})"


# MARK: - Tools

class AgentTools:
    def __init__(self, host: AgentHost):
        self.host = host

    @property
    def app(self):
        return self.host.app

    async def call(self, name: str, arguments: dict, client: AgentClient) -> ToolResult:
        """Runs one tool call and writes the audit line."""
        started = time.time()
        clock = time.monotonic()
        call = ToolCall()
        try:
            result = await self._run(name, ToolArguments(arguments), client, call)
        except ToolArgumentError as error:
            result = ToolResult.error(error.message)
        except ToolFailure as error:
            result = ToolResult.error(error.message)
        except Exception as error:  # noqa: BLE001 - the model gets the message, the log the trace
            log.exception("tool %s failed", name)
            result = ToolResult.error(str(error) or type(error).__name__)
        error_text = None
        if result.is_error:
            error_text = next((block.text for block in result.content if isinstance(block, TextContent)), None)
        self.host.log.record(AuditEntry(date=started, client=client.name, connection=call.connection, tool=name,
                                        arguments=arguments, error=error_text, duration=time.monotonic() - clock))
        return result

    async def _run(self, name: str, arguments: ToolArguments, client: AgentClient, call: ToolCall) -> ToolResult:
        if name == "list_connections":
            return self._list_connections(client)
        if name == "list_sessions":
            return self._list_sessions(client)
        if name == "connect":
            return await self._connect(arguments, client, call)
        if name == "disconnect":
            return self._disconnect(arguments, client, call)
        record = await self._session(arguments, client, call)

        async def body() -> ToolResult:
            try:
                return await self._run_on_session(name, arguments, record)
            except ToolArgumentError as error:
                return ToolResult.error(error.message)
            except ToolFailure as error:
                return ToolResult.error(error.message)
        return await record.queue.run(body)

    async def _run_on_session(self, name: str, arguments: ToolArguments, record: AgentSessionRecord) -> ToolResult:
        rdp = self._connected_session(record)
        frame = rdp.peek_frame()
        mapping = self._mapping(rdp, frame)
        if name == "screenshot":
            return await self._screenshot(record, frame, mapping, arguments.optional_rect("region"))
        if name == "click":
            result = await self._click(arguments, record, rdp, mapping)
        elif name == "mouse_move":
            point = self._remote_point(arguments, mapping)
            rdp.send_mouse_move(*point)
            record.pointer = point
            record.note_input()
            result = ToolResult.text(f"Moved the pointer to {describe_point(point, mapping)}.")
        elif name in ("mouse_down", "mouse_up"):
            result = await self._press(arguments, name == "mouse_down", record, rdp, mapping)
        elif name == "drag":
            result = await self._drag(arguments, record, rdp, mapping)
        elif name == "scroll":
            result = await self._scroll(arguments, record, rdp, mapping)
        elif name == "type_text":
            result = await self._type_text(arguments, record, rdp)
        elif name == "key":
            result = await self._key(arguments, record, rdp)
        elif name == "clipboard_get":
            return await self._clipboard_get(record)
        elif name == "clipboard_set":
            return await self._clipboard_set(arguments, record)
        elif name == "wait":
            seconds = arguments.number("seconds", 0, 30)
            await sleep(seconds)
            return ToolResult.text(f"Waited {format_number(seconds)} s.")
        elif name == "wait_for_screen":
            timeout = arguments.number("timeout", 0, 60, 10)
            settle = arguments.integer("settle_ms", 100, 5000, 500)
            return ToolResult.text(await self._wait_for_screen(record, timeout, settle / 1000))
        elif name == "read_screen":
            return await self._read_screen(arguments, frame, mapping)
        elif name == "find_text":
            return await self._find_text(arguments, frame, mapping)
        else:
            raise ToolFailure(f"Unknown tool '{name}'.")
        if not arguments.bool("screenshot", False):
            return result
        settled = await self._wait_for_screen(record, 5, 0.5)
        current = self._connected_session(record)
        frame = current.peek_frame()
        shot = await self._screenshot(record, frame, self._mapping(current, frame), None)
        return ToolResult(result.content + [TextContent(settled)] + shot.content)

    @staticmethod
    def _mapping(rdp, frame) -> ScreenMapping:
        # The framebuffer knows the real size (a server may ignore the size asked for).
        width, height = (frame.width, frame.height) if frame is not None else rdp.desktop_size
        return ScreenMapping(width, height)

    # MARK: Connections and sessions

    def _rdp_connections(self) -> list:
        return [c for c in self.app.library.search("", "name") if c.protocol is ConnectionProtocol.RDP]

    def _page_for_connection(self, connection_id: str):
        return next((page for page in self.app.session_pages() if page.connection_id == connection_id), None)

    def _list_connections(self, client: AgentClient) -> ToolResult:
        items = []
        for connection in self._rdp_connections():
            item = {"name": connection.name, "id": connection.id, "host": host_and_port(connection),
                    "agent_access": self.host.policy(connection).value}
            page = self._page_for_connection(connection.id)
            if page is not None:
                item["open_session"] = page.session_id
                item["session_owner"] = self._owner(page, client)
            items.append(item)
        if not items:
            return ToolResult.text("There are no saved RDP connections.")
        return ToolResult.text("Saved RDP connections. agent_access: ask = the user is asked to allow it, "
                               "always = allowed, never = refused.\n" + encode_json(items))

    def _list_sessions(self, client: AgentClient) -> ToolResult:
        items = []
        for page in self.app.session_pages():
            if page.connection.protocol is not ConnectionProtocol.RDP:
                continue
            record = self.host.records.get(page.session_id)
            item = {"session": page.session_id, "connection": page.connection.name,
                    "host": host_and_port(page.connection), "owner": self._owner(page, client),
                    "status": status_name(page.status)}
            rdp = page.connected_session
            if rdp is not None:
                mapping = self._mapping(rdp, rdp.peek_frame())
                item["screenshot_size"] = f"{mapping.screenshot_width}×{mapping.screenshot_height}"
                item["scale"] = mapping.scale
            if record is not None and record.client_id == client.id and page.agent_control is not None \
                    and page.agent_control.paused:
                item["paused_by_user"] = True
            items.append(item)
        for record in self.host.records.values():
            if record.client_id == client.id and record.ended is not None:
                items.append({"session": record.id, "status": "ended", "reason": record.ended})
        if not items:
            return ToolResult.text("No sessions are open. Use connect.")
        return ToolResult.text("Open sessions. owner: you = yours, user = the user's own session (using its id "
                               "asks the user and lets you work in it), other agent = not available.\n"
                               + encode_json(items))

    async def _connect(self, arguments: ToolArguments, client: AgentClient, call: ToolCall) -> ToolResult:
        connection = self._connection_named(arguments.non_empty_string("connection"))
        call.connection = label(connection)
        if connection.protocol is not ConnectionProtocol.RDP:
            raise ToolFailure(f"“{connection.name}” is a VNC connection; agents can only use RDP connections.")
        width = arguments.integer("width", 800, 1920, 1280) & ~1
        height = arguments.integer("height", 600, 1200, 800)
        show = arguments.bool("show", False)

        running = self._page_for_connection(connection.id)
        if running is not None:
            record = self.host.records.get(running.session_id)
            if record is not None and record.page is running and record.ended is None:
                if record.client_id != client.id:
                    raise ToolFailure(f"Another agent is working in “{connection.name}” right now.")
                if show:
                    self.app.show_session_page(running, activate=False)
                return ToolResult.text("Already connected: " + self._describe(record))
            # The user has it open: a second logon would throw the user out, so work in theirs.
            record = await self._attach(running, client)
            if show:
                self.app.show_session_page(running, activate=False)
            return ToolResult.text("Attached to the user's open session (the user sees what you do): "
                                   + self._describe(record))

        if not connection.security.disable_nla and (
                not connection.username or self.app.library.password(connection.id) is None):
            raise ToolFailure(f"No user name or password is saved for “{connection.name}”. Ask the user to connect "
                              "once by hand in Weitblick Remote and save the password.")
        session_id = self.host.make_session_id()
        await self._check_access(connection, client, session_id)
        if client.id not in self.host.clients:
            raise ToolFailure("The client disconnected.")
        if self._page_for_connection(connection.id) is not None:
            raise ToolFailure(f"“{connection.name}” was opened while waiting for permission. Call connect again.")
        latest = self.app.library.connection(connection.id) or connection
        page = self.app.open_agent_session(latest, width, height, show, session_id)
        from .host import AgentSessionRecord
        record = AgentSessionRecord(session_id, page, client.id, owned=True)
        self.host.register(record)
        page.attach_agent(client.name)
        failure = await from_callback(lambda done: page.wait_until_connected(CONNECT_TIMEOUT, done))
        if failure is not None:
            self.host.unregister(session_id)
            if not page.is_shown:
                self.app.close_session_page(page)
            raise ToolFailure(failure)
        ready = await self._wait_for_desktop(record)
        return ToolResult.text(
            "Connected: " + self._describe(record)
            + (" Take a screenshot to see it." if ready else
               " The screen is still blank (Windows may still be signing in): wait_for_screen, then take a "
               "screenshot."))

    async def _wait_for_desktop(self, record: AgentSessionRecord) -> bool:
        """After the logon Windows paints a flat white, black or blue screen for a while: wait until
        there is a picture and it has settled, so the first screenshot shows the desktop."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rdp = record.page.connected_session
            if rdp is None:
                return False
            if not screen_capture.looks_blank(rdp.peek_frame()):
                await self._settle(lambda: record.page.connected_session, settle=1, timeout=10)
                return True
            await sleep(0.25)
        return False

    def _disconnect(self, arguments: ToolArguments, client: AgentClient, call: ToolCall) -> ToolResult:
        session_id = arguments.string("session")
        record = self.host.records.get(session_id)
        if record is None or record.client_id != client.id:
            raise ToolFailure(f"Session '{session_id}' is not one of yours (list_sessions shows them).")
        call.connection = label(record.page.connection)
        if record.ended is not None:
            self.host.unregister(session_id)
            return ToolResult.text(f"Session {session_id} had already ended.")
        page = record.page
        kept_by_user = not record.owned or (page.agent_control is not None and page.agent_control.paused
                                            and page.is_shown)
        self.host.release(record, "the agent disconnected")
        return ToolResult.text(f"Released session {session_id}; it stays open for the user." if kept_by_user
                               else f"Disconnected session {session_id}.")

    async def _session(self, arguments: ToolArguments, client: AgentClient, call: ToolCall) -> AgentSessionRecord:
        """The session a tool names, checked against the gate on every call."""
        session_id = arguments.string("session")
        record = self.host.records.get(session_id)
        if record is not None:
            if record.client_id != client.id:
                raise ToolFailure(f"Another agent is working in session {session_id}.")
            connection = self.app.library.connection(record.page.connection_id) or record.page.connection
            call.connection = label(connection)
            if record.ended is not None:
                raise ToolFailure(f"Session {session_id} has ended: {record.ended} Connect again.")
            await self._check_access(connection, client, session_id)
            if self.host.records.get(session_id) is not record or record.ended is not None:
                raise ToolFailure(f"Session {session_id} has ended.")
            if record.page.agent_control is not None and record.page.agent_control.paused:
                raise ToolFailure(f"Paused by the user: they took over session {session_id}. Wait and try again "
                                  "later; the user can click “Resume agent”.")
            record.last_used = time.monotonic()
            return record
        page = next((page for page in self.app.session_pages() if page.session_id == session_id), None)
        if page is None:
            raise ToolFailure(f"Unknown session '{session_id}'. list_sessions shows the open sessions.")
        call.connection = label(page.connection)
        return await self._attach(page, client)

    async def _attach(self, page, client: AgentClient) -> AgentSessionRecord:
        """Takes control of a session the user has open, after asking."""
        connection = self.app.library.connection(page.connection_id) or page.connection
        if page.connection.protocol is not ConnectionProtocol.RDP:
            raise ToolFailure("Agents can only use RDP sessions.")
        if page.agent_control is not None:
            raise ToolFailure(f"Another agent is working in “{connection.name}”.")
        if page.status.kind != "connected":
            raise ToolFailure(f"The user's session on “{connection.name}” is not connected "
                              f"({status_name(page.status)}).")
        await self._check_access(connection, client, page.session_id)
        if client.id not in self.host.clients:
            raise ToolFailure("The client disconnected.")
        if page not in self.app.session_pages() or page.agent_control is not None:
            raise ToolFailure(f"The session on “{connection.name}” changed while waiting for permission. Try again.")
        from .host import AgentSessionRecord
        record = AgentSessionRecord(page.session_id, page, client.id, owned=False)
        self.host.register(record)
        page.attach_agent(client.name)
        self.host.log.event(f"session {record.id} attached: the user's session on \"{connection.name}\"")
        return record

    async def _check_access(self, connection, client: AgentClient, session_id: str) -> None:
        request = self.host.request(connection, client, session_id)
        decision = await from_callback(
            lambda done: self.host.gate.check(request, self.host.policy(connection), done))
        if not decision.allowed:
            raise ToolFailure(decision.message or "Access was refused.")

    def _connected_session(self, record: AgentSessionRecord):
        if record.ended is not None:
            raise ToolFailure(f"Session {record.id} has ended: {record.ended}")
        rdp = record.page.connected_session
        if rdp is None:
            raise ToolFailure(f"Session {record.id} is {status_name(record.page.status)} right now. Wait a moment "
                              "and try again.")
        return rdp

    def _connection_named(self, query: str):
        connections = self.app.library.connections
        by_id = next((c for c in connections if c.id == query.strip().upper()), None)
        if by_id is not None:
            return by_id
        matches = [c for c in connections if c.name.casefold() == query.strip().casefold()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ToolFailure(f"Several connections are named “{query}”; use the id from list_connections.")
        raise ToolFailure(f"No connection named “{query}”. list_connections shows the saved connections.")

    def _owner(self, page, client: AgentClient) -> str:
        record = self.host.records.get(page.session_id)
        if record is not None and record.page is page:
            return "you" if record.client_id == client.id else "other agent"
        return "user" if page.agent_control is None else "other agent"

    def _describe(self, record: AgentSessionRecord) -> str:
        connection = record.page.connection
        text = f"session {record.id} on “{connection.name}” ({connection.host})."
        rdp = record.page.connected_session
        if rdp is not None:
            text += f" Screenshots are {self._mapping(rdp, rdp.peek_frame()).summary}."
        return text

    # MARK: Screen

    async def _screenshot(self, record: AgentSessionRecord, frame, mapping: ScreenMapping,
                          region: PixelRect | None) -> ToolResult:
        desktop_rect = mapping.desktop_rect(region) if region is not None else None
        pixels = screen_capture.crop(frame, desktop_rect) if frame is not None else None
        if pixels is None:
            raise ToolFailure(f"Session {record.id} has no picture yet. Wait a moment and try again.")
        width, height = screenshot_size(pixels.rect, mapping)
        try:
            data, mime_type = await run_in_thread(screen_capture.screenshot, pixels, width, height)
        except Exception as error:  # noqa: BLE001
            raise ToolFailure("The screenshot couldn’t be encoded.") from error
        if region is not None:
            origin = mapping.screenshot_point(pixels.rect.x, pixels.rect.y)
            text = (f"Region of the screen: {width}×{height} px whose top left is "
                    f"({int(round_half_away(origin.x))}, {int(round_half_away(origin.y))}) on the "
                    f"{mapping.screenshot_width}×{mapping.screenshot_height} screen (scale "
                    f"{format_number(mapping.scale, 4)}). For clicks add the region's top left to the image pixel.")
        else:
            text = f"Screenshot {mapping.summary}."
        if record.pointer is not None:
            text += f" Pointer at {describe_point(record.pointer, mapping)}."
        return ToolResult([ImageContent(base64.b64encode(data).decode("ascii"), mime_type), TextContent(text)])

    async def _settle(self, current_session, settle: float, timeout: float, interval: float = 0.1):
        """Polls framebuffer copies until nothing significant changed for `settle` seconds. Returns
        ("settled", seconds), ("busy", last change or None) or ("gone", None). Works for windowless
        sessions too."""
        start = time.monotonic()
        tracker = SettleTracker(settle, 0)
        previous = None
        last_change = None
        while True:
            session = current_session()
            if session is None:
                return ("gone", None)
            elapsed = time.monotonic() - start
            frame = session.peek_frame()
            changed = False
            if frame is not None:
                if previous is not None and (previous.width, previous.height, previous.stride) == \
                        (frame.width, frame.height, frame.stride):
                    bounds = changed_bounds(previous.data, frame.data, frame.width, frame.height, frame.stride)
                    if is_significant(bounds):
                        changed = True
                        last_change = bounds
                else:
                    changed = previous is not None
                previous = frame
            if tracker.update(changed, elapsed):
                return ("settled", elapsed)
            if elapsed >= timeout:
                return ("busy", last_change)
            await sleep(interval)

    async def _wait_for_screen(self, record: AgentSessionRecord, timeout: float, settle: float) -> str:
        outcome, value = await self._settle(lambda: record.page.connected_session, settle, timeout)
        if outcome == "settled":
            return f"The screen is quiet (settled after {format_number(value)} s)."
        if outcome == "gone":
            return "The session is not connected any more."
        text = f"The screen was still changing after {format_number(timeout)} s"
        rdp = record.page.connected_session
        if value is not None and rdp is not None:
            mapping = self._mapping(rdp, rdp.peek_frame())
            box = mapping.screenshot_rect(PixelRect(value.x, value.y, value.width, value.height))
            text += f" (last change around {describe_box(box)})"
        return text + "."

    async def _recognize(self, arguments: ToolArguments, frame, mapping: ScreenMapping) -> list:
        region = arguments.optional_rect("region")
        desktop_rect = mapping.desktop_rect(region) if region is not None else None
        pixels = screen_capture.crop(frame, desktop_rect) if frame is not None else None
        if pixels is None:
            raise ToolFailure("The session has no picture yet.")
        factor = upscale_factor(pixels.rect.width, pixels.rect.height)
        png = await run_in_thread(screen_capture.ocr_png, pixels, factor)
        lines, error = await from_callback(lambda done: recognize_async(
            png, done, offset=(pixels.rect.x, pixels.rect.y), scale=factor))
        if error is not None:
            raise ToolFailure(f"Text recognition failed: {error}")
        return lines or []

    async def _read_screen(self, arguments: ToolArguments, frame, mapping: ScreenMapping) -> ToolResult:
        lines = await self._recognize(arguments, frame, mapping)
        if not lines:
            return ToolResult.text("No text recognized.")
        rows = []
        for line in lines:
            box = mapping.screenshot_rect(PixelRect(*line.box))
            rows.append(f"“{line.text}” {describe_box(box)} {click_point(box)}")
        return ToolResult.text(f"{len(lines)} lines of text (screenshot pixels: box x, y, width×height; click = box "
                               "center):\n" + "\n".join(rows))

    async def _find_text(self, arguments: ToolArguments, frame, mapping: ScreenMapping) -> ToolResult:
        query = arguments.non_empty_string("query")
        lines = await self._recognize(arguments, frame, mapping)
        matches = text_matching.find(query, [line.text for line in lines])
        if not matches:
            seen = ", ".join(f"“{line.text}”" for line in lines[:40])
            return ToolResult.text(f"No match for “{query}”." + (f" Text on screen: {seen}" if seen
                                                                  else " No text recognized."))
        rows = []
        for index, match in enumerate(matches, start=1):
            line = lines[match.line_index]
            found = line.text[match.start:match.end]
            box = mapping.screenshot_rect(PixelRect(*(line.box_of_range(match.start, match.end) or line.box)))
            row = f"{index}. “{found}”"
            if found != line.text:
                row += f" in “{line.text}”"
            row += f" {describe_box(box)} {click_point(box)}"
            if match.errors > 0:
                row += f", approximate ({match.errors} differences)"
            rows.append(row)
        return ToolResult.text(f"{len(matches)} matches for “{query}” (screenshot pixels, best first):\n"
                               + "\n".join(rows))

    # MARK: Mouse

    @staticmethod
    def _remote_point(arguments: ToolArguments, mapping: ScreenMapping) -> tuple[int, int]:
        return mapping.desktop_point(PixelPoint(arguments.number("x"), arguments.number("y")))

    @staticmethod
    def _mouse_button(arguments: ToolArguments) -> MouseButton:
        return {"right": MouseButton.RIGHT, "middle": MouseButton.MIDDLE}.get(
            arguments.choice("button", ["left", "right", "middle"], "left"), MouseButton.LEFT)

    async def _send(self, actions: list[KeyAction], rdp) -> None:
        for action in actions:
            send_actions(rdp, [action])
            await sleep(KEY_PACE)

    async def _click(self, arguments: ToolArguments, record: AgentSessionRecord, rdp,
                     mapping: ScreenMapping) -> ToolResult:
        point = self._remote_point(arguments, mapping)
        button = self._mouse_button(arguments)
        count = arguments.integer("count", 1, 3, 1)
        modifiers = HeldModifiers.parse(arguments.string_array("modifiers"))
        await self._send(HeldModifiers.press(modifiers), rdp)
        rdp.send_mouse_move(*point)
        await sleep(0.02)
        for _ in range(count):
            rdp.send_mouse_button(button, True, *point)
            await sleep(0.03)
            rdp.send_mouse_button(button, False, *point)
            await sleep(0.05)
        await self._send(HeldModifiers.release(modifiers), rdp)
        record.pointer = point
        record.note_input()
        what = {1: "Clicked", 2: "Double-clicked"}.get(count, "Triple-clicked")
        return ToolResult.text(f"{what} at {describe_point(point, mapping)}.")

    async def _press(self, arguments: ToolArguments, down: bool, record: AgentSessionRecord, rdp,
                     mapping: ScreenMapping) -> ToolResult:
        button = self._mouse_button(arguments)
        point = record.pointer or (mapping.desktop_width // 2, mapping.desktop_height // 2)
        if arguments.has("x") or arguments.has("y"):
            point = self._remote_point(arguments, mapping)
            rdp.send_mouse_move(*point)
            await sleep(0.02)
        rdp.send_mouse_button(button, down, *point)
        record.pointer = point
        record.note_input()
        return ToolResult.text(f"{'Pressed' if down else 'Released'} the mouse button at "
                               f"{describe_point(point, mapping)}.")

    async def _drag(self, arguments: ToolArguments, record: AgentSessionRecord, rdp,
                    mapping: ScreenMapping) -> ToolResult:
        path = arguments.points("path")
        if len(path) < 2:
            raise ToolArgumentError("'path' needs at least two points.")
        points = [mapping.desktop_point(point) for point in path]
        button = self._mouse_button(arguments)
        rdp.send_mouse_move(*points[0])
        await sleep(0.03)
        rdp.send_mouse_button(button, True, *points[0])
        await sleep(0.06)
        for start, end in zip(points, points[1:]):
            for step in drag_steps(start, end):
                rdp.send_mouse_move(*step)
                await sleep(0.01)
        await sleep(0.06)
        rdp.send_mouse_button(button, False, *points[-1])
        record.pointer = points[-1]
        record.note_input()
        return ToolResult.text(f"Dragged from {describe_point(points[0], mapping)} to "
                               f"{describe_point(points[-1], mapping)}.")

    async def _scroll(self, arguments: ToolArguments, record: AgentSessionRecord, rdp,
                      mapping: ScreenMapping) -> ToolResult:
        point = self._remote_point(arguments, mapping)
        dx = arguments.integer("dx", -50, 50, 0)
        dy = arguments.integer("dy", -50, 50, 0)
        if dx == 0 and dy == 0:
            raise ToolArgumentError("Give dx or dy (wheel notches).")
        rdp.send_mouse_move(*point)
        await sleep(0.02)
        for vertical, horizontal in wheel_events(dx, dy):
            rdp.send_mouse_wheel(vertical, horizontal, *point)
            await sleep(0.015)
        record.pointer = point
        record.note_input()
        return ToolResult.text(f"Scrolled {scroll_description(dx, dy)} at {describe_point(point, mapping)}.")

    # MARK: Keyboard

    async def _type_text(self, arguments: ToolArguments, record: AgentSessionRecord, rdp) -> ToolResult:
        text = arguments.string("text")
        if len(text) > MAX_TYPED_CHARACTERS:
            raise ToolArgumentError(f"'text' is longer than {MAX_TYPED_CHARACTERS} characters; use clipboard_set "
                                    "and ctrl+v.")
        for group in typing_actions(text):
            send_actions(rdp, group)
            await sleep(CHARACTER_PACE)
        record.note_input()
        return ToolResult.text(f"Typed {len(text)} characters.")

    async def _key(self, arguments: ToolArguments, record: AgentSessionRecord, rdp) -> ToolResult:
        keys = arguments.string("keys")
        chords = keycombo.parse(keys)
        count = arguments.integer("repeat", 1, 100, 1)
        layout = rdp.config.keyboard_layout
        keycombo.actions(chords, layout)  # refuse unknown keys before anything is pressed
        for _ in range(count):
            for chord in chords:
                await self._send(keycombo.actions([chord], layout), rdp)
                await sleep(CHORD_PACE)
        record.note_input()
        return ToolResult.text(f"Pressed {keys}{f' {count} times' if count > 1 else ''}.")

    # MARK: Clipboard

    def _clipboard(self, record: AgentSessionRecord):
        clipboard = record.page.clipboard
        name = record.page.connection.name
        if clipboard is None:
            raise ToolFailure(f"The clipboard isn't available in this session: clipboard redirection is off for "
                              f"“{name}” (the user can turn it on in the connection's Redirection settings).")
        if not clipboard.is_channel_ready:
            raise ToolFailure(f"The clipboard of session {record.id} isn't ready: the server hasn't opened its "
                              "clipboard channel (it may not support clipboard redirection).")
        return clipboard

    async def _clipboard_get(self, record: AgentSessionRecord) -> ToolResult:
        clipboard = self._clipboard(record)
        # A copy (ctrl+c) right before arrives as a clipboard change shortly after the keys.
        if record.last_input is not None and clipboard.remote_change_count == record.remote_changes_at_last_input:
            deadline = record.last_input + CLIPBOARD_CHANGE_WAIT
            while time.monotonic() < deadline and clipboard.remote_change_count == record.remote_changes_at_last_input:
                await sleep(0.05)
        text = await from_callback(lambda done: clipboard.get_remote_text(done))
        if text is None:
            return ToolResult.text("(The remote clipboard holds no text.)")
        return ToolResult.text(text if text else "(The remote clipboard text is empty.)")

    async def _clipboard_set(self, arguments: ToolArguments, record: AgentSessionRecord) -> ToolResult:
        text = arguments.string("text")
        clipboard = self._clipboard(record)
        accepted = await from_callback(lambda done: clipboard.set_text(text, done))
        if not accepted:
            raise ToolFailure("The session did not take the clipboard text. Try again.")
        return ToolResult.text(f"The remote clipboard now holds {len(text)} characters. Paste with key ctrl+v.")
