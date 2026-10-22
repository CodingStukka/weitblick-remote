"""Serves MCP for agents: the socket, its clients, the permission gate, the sessions agents use and
the audit log (port of AgentHost.swift). The app creates it while Settings → "AI agents (MCP)" is on
and stops it when that is turned off; off means no socket at all.

Test hooks (environment, like the macOS app's debug launch arguments):
- WEITBLICK_AGENT_PROMPT_ANSWERS: comma-separated answers (deny, once, ten-minutes, today, always) for
  the next permission prompts instead of showing them; once used up, nobody answers (timeout).
- WEITBLICK_AGENT_PROMPT_TIMEOUT, WEITBLICK_AGENT_GRANT_SECONDS: shorter prompt timeout and "10 minutes".
- WEITBLICK_AGENT_IDLE_QUIT: seconds after which an app without windows, clients and sessions quits
  (default 60).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

from gi.repository import GLib

from .. import VERSION
from ..store.connection import AgentAccess
from .access_gate import AccessGate, AccessGrant, AccessPrompt, AccessRequest, AgentAccessPolicy, PromptAnswer
from .audit import AuditLog
from .catalog import INSTRUCTIONS, TOOLS
from .loop import SerialQueue, spawn
from .mcp_core import CallTool, MCPServerCore, Reply, ToolResult
from .socket_server import AgentSocketConnection, AgentSocketServer, SocketServerError
from .tools import AgentTools

log = logging.getLogger(__name__)

#: Agent sessions without a tool call for this long close (attached ones are released).
IDLE_LIMIT = 30 * 60
#: An app nobody needs (no window, no client, no session) quits after this long.
AGENT_LAUNCH_IDLE_QUIT = 60
HOUSEKEEPING_SECONDS = 15


def _env_float(name: str) -> float | None:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return None


class AgentClient:
    """An MCP client on the socket (one weitblick-mcp process)."""

    def __init__(self, socket: AgentSocketConnection):
        self.id = socket.id
        self.socket = socket
        self.core = MCPServerCore("weitblick-remote", VERSION, INSTRUCTIONS, TOOLS)

    @property
    def name(self) -> str:
        return self.core.client.display_name


class AgentSessionRecord:
    """A session an agent works in: one it opened, or a session of the user it attached to."""

    def __init__(self, session_id: str, page, client_id: int, owned: bool):
        self.id = session_id
        self.page = page
        self.client_id = client_id
        #: Opened by the agent (closed with it); else the user's session (released, stays open).
        self.owned = owned
        self.last_used = time.monotonic()
        #: Why the session is gone (the next tool call reports it).
        self.ended: str | None = None
        #: Where the agent last put the pointer (desktop pixels).
        self.pointer: tuple[int, int] | None = None
        #: For clipboard_get right after a copy: the input time and the clipboard changes before it.
        self.last_input: float | None = None
        self.remote_changes_at_last_input = 0
        #: Tool calls on this session run one after another (parallel calls must not mix input).
        self.queue = SerialQueue()

    def note_input(self) -> None:
        self.last_input = time.monotonic()
        clipboard = self.page.clipboard
        self.remote_changes_at_last_input = clipboard.remote_change_count if clipboard is not None else 0


class AgentHost:
    def __init__(self, app):
        self.app = app
        self.log = AuditLog()
        gate_options = {}
        if (timeout := _env_float("WEITBLICK_AGENT_PROMPT_TIMEOUT")) is not None:
            gate_options["prompt_timeout"] = timeout
        if (grant := _env_float("WEITBLICK_AGENT_GRANT_SECONDS")) is not None:
            gate_options["timed_grant_duration"] = grant
        self.gate = AccessGate(**gate_options)
        self.gate.show_prompt = self._prompt_opened
        self.gate.close_prompt = self._prompt_closed
        self.gate.persist_always_allow = self._persist_always_allow
        self.gate.on_change = self._grants_changed
        self.tools = AgentTools(self)
        self.clients: dict[int, AgentClient] = {}
        self.records: dict[str, AgentSessionRecord] = {}
        self.start_error: str | None = None
        self._server: AgentSocketServer | None = None
        self._housekeeping = 0
        self._idle_since = time.monotonic()
        self._idle_quit = _env_float("WEITBLICK_AGENT_IDLE_QUIT") or AGENT_LAUNCH_IDLE_QUIT
        #: "Always allow" answers until the store has them.
        self._always_allow_pending: set[str] = set()
        self._grant_listeners: dict[int, Callable[[], None]] = {}
        self._next_listener = 1
        self._next_session = 1
        self._prompts = None
        answers = os.environ.get("WEITBLICK_AGENT_PROMPT_ANSWERS")
        self._scripted_answers: list[PromptAnswer] | None = (
            [PromptAnswer(item.strip()) for item in answers.split(",") if item.strip()] if answers is not None
            else None)

    @property
    def is_running(self) -> bool:
        return self._server is not None

    # MARK: Master switch

    def start(self) -> None:
        if self._server is not None:
            return
        server = AgentSocketServer(self._accept)
        try:
            server.start()
        except SocketServerError as error:
            self.start_error = str(error)
            log.error("MCP socket not started: %s", error)
            self._grants_changed()
            return
        self._server = server
        self.start_error = None
        self._idle_since = time.monotonic()
        self.log.event(f"MCP server started on {server.path}")
        self._housekeeping = GLib.timeout_add_seconds(HOUSEKEEPING_SECONDS, self._tidy_up)
        self._grants_changed()

    def stop(self) -> None:
        """The master switch went off or the app quits: no socket, no grants, no agent sessions."""
        if self._housekeeping:
            GLib.source_remove(self._housekeeping)
            self._housekeeping = 0
        self.start_error = None
        server, self._server = self._server, None
        if server is None:
            return
        server.stop()
        for client in list(self.clients.values()):
            client.socket.close()
            self._client_gone(client)
        self.gate.cancel_all_prompts("AI agents were turned off in Weitblick Remote.")
        self.gate.revoke_all()
        self.log.event("MCP server stopped")
        self.log.flush(2)

    # MARK: Clients

    def _accept(self, socket: AgentSocketConnection) -> None:
        if self._server is None:
            socket.close()
            return
        client = AgentClient(socket)
        self.clients[client.id] = client
        socket.start(lambda line: self._handle(line, client), lambda: self._client_gone(client))

    def _handle(self, line: str, client: AgentClient) -> None:
        was_initialized = client.core.protocol_version is not None
        action = client.core.handle_line(line)
        if not was_initialized and client.core.protocol_version is not None:
            self.log.event(f"client {client.id} connected: \"{client.name}\" (MCP {client.core.protocol_version})")
        if isinstance(action, Reply):
            client.socket.send(action.line)
        elif isinstance(action, CallTool):
            task = spawn(self.tools.call(action.name, action.arguments, client))

            def answer(finished) -> None:
                error = finished.exception()
                result = finished.result() if error is None else ToolResult.error(str(error))
                client.socket.send(MCPServerCore.response(action.id, result))
            task.add_done_callback(answer)

    def _client_gone(self, client: AgentClient) -> None:
        if self.clients.pop(client.id, None) is None:
            return
        for record in list(self.records.values()):
            if record.client_id == client.id:
                self.release(record, "its client disconnected")
        self._idle_since = time.monotonic()
        self.log.event(f"client {client.id} disconnected: \"{client.name}\"")

    # MARK: Sessions

    def make_session_id(self) -> str:
        taken = {page.session_id for page in self.app.session_pages()} | set(self.records)
        while f"s{self._next_session}" in taken:
            self._next_session += 1
        session_id = f"s{self._next_session}"
        self._next_session += 1
        return session_id

    def release(self, record: AgentSessionRecord, reason: str) -> None:
        """Ends the agent's use of a session: its own session closes (unless the user took it over
        and watches it), a user's session is released."""
        self.records.pop(record.id, None)
        self.gate.session_ended(record.id)
        if record.ended is not None:
            return
        page = record.page
        kept = page.agent_control is not None and page.agent_control.paused and page.is_shown
        if record.owned and not kept:
            self.log.event(f"session {record.id} closed: {reason}")
            record.ended = reason
            self.app.close_session_page(page)
        else:
            self.log.event(f"session {record.id} released: {reason}")
            page.detach_agent()

    def register(self, record: AgentSessionRecord) -> None:
        self.records[record.id] = record

    def unregister(self, session_id: str) -> None:
        self.records.pop(session_id, None)
        self.gate.session_ended(session_id)

    def page_closed(self, page) -> None:
        """A session page closed (by the user, the server or the agent)."""
        record = self.records.get(page.session_id)
        if record is None or record.page is not page:
            return
        if record.ended is None:
            record.ended = page.agent_end_message or "The session was closed."
        self.gate.session_ended(record.id)

    def _tidy_up(self) -> bool:
        """Every 15 s: idle sessions, and quitting an app nobody needs."""
        now = time.monotonic()
        for record in list(self.records.values()):
            if record.ended is None and now - record.last_used > IDLE_LIMIT:
                self.release(record, "30 minutes without a tool call")
        if self.clients or self.app.session_pages() or self.app.get_windows():
            self._idle_since = now
        elif now - self._idle_since > self._idle_quit:
            self.log.event("no agent needs the app any more: quitting")
            self.log.flush(2)
            self._housekeeping = 0
            self.app.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    # MARK: Permission

    def policy(self, connection) -> AgentAccessPolicy:
        """The connection's agent access as the gate sees it."""
        if connection.id in self._always_allow_pending:
            return AgentAccessPolicy.ALWAYS
        try:
            return AgentAccessPolicy(connection.security.agent_access.value)
        except ValueError:
            return AgentAccessPolicy.ASK

    def request(self, connection, client: AgentClient, session_id: str) -> AccessRequest:
        return AccessRequest(connection.id, connection.name, connection.host, client.name, session_id)

    def _prompt_opened(self, prompt: AccessPrompt) -> None:
        request = prompt.request
        self.log.event(f"prompt {prompt.id} opened: client=\"{request.client}\" "
                       f"connection=\"{request.connection_name} ({request.host})\"")
        if self._scripted_answers is not None:
            # Tests: answer from the environment; none left = nobody answers (timeout).
            if not self._scripted_answers:
                return
            answer = self._scripted_answers.pop(0)

            def scripted() -> bool:
                self.log.event(f"prompt {prompt.id} answered: {answer.value} (scripted)")
                self.gate.answer(prompt.id, answer)
                return GLib.SOURCE_REMOVE
            GLib.timeout_add(200, scripted)
            return
        self._prompt_windows().show(prompt)

    def _prompt_closed(self, prompt: AccessPrompt) -> None:
        if self._prompts is not None:
            self._prompts.close(prompt)

    def _prompt_windows(self):
        if self._prompts is None:
            from .ui import PromptWindows
            self._prompts = PromptWindows(self.app, self._answer_prompt)
        return self._prompts

    def _answer_prompt(self, prompt_id: int, answer: PromptAnswer) -> None:
        self.log.event(f"prompt {prompt_id} answered: {answer.value}")
        self.gate.answer(prompt_id, answer)

    def _persist_always_allow(self, connection_id: str) -> None:
        self._always_allow_pending.add(connection_id)

        def change(connection) -> None:
            connection.security.agent_access = AgentAccess.ALWAYS
        try:
            self.app.library.modify(connection_id, change)
        except Exception:  # noqa: BLE001
            log.exception("saving Always allow failed")
        finally:
            self._always_allow_pending.discard(connection_id)

    # MARK: Grants (settings list)

    def add_grants_listener(self, callback: Callable[[], None]) -> int:
        token = self._next_listener
        self._next_listener += 1
        self._grant_listeners[token] = callback
        return token

    def remove_grants_listener(self, token: int) -> None:
        self._grant_listeners.pop(token, None)

    def _grants_changed(self) -> None:
        for callback in list(self._grant_listeners.values()):
            callback()

    def current_grants(self) -> list[AccessGrant]:
        """Grants expire silently; the settings list asks before drawing."""
        return self.gate.active_grants

    def revoke(self, grant: AccessGrant) -> None:
        """Settings → Revoke."""
        self.gate.revoke(grant.id)
        self.log.event(f"grant for \"{grant.connection_name}\" revoked by the user")
