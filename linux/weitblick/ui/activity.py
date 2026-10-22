"""What the overview shows about open sessions: status per connection and agents at work."""

from __future__ import annotations

from dataclasses import dataclass

from gi.repository import GObject

from ..i18n import _


@dataclass(frozen=True)
class SessionStatus:
    """kind: "connecting", "connected", "reconnecting" (with attempt) or "disconnected"."""

    kind: str
    attempt: int = 0

    @property
    def label(self) -> str:
        if self.kind == "connecting":
            return _("Connecting…")
        if self.kind == "connected":
            return _("Connected")
        if self.kind == "reconnecting":
            return _("Reconnecting (attempt {n})…").format(n=self.attempt)
        return _("Disconnected")


CONNECTING = SessionStatus("connecting")
CONNECTED = SessionStatus("connected")
DISCONNECTED = SessionStatus("disconnected")


@dataclass(frozen=True)
class AgentActivity:
    """An AI agent working in a connection's session (overview badge and "Watch")."""

    #: The MCP client, e.g. "claude-code 2.1.306".
    client: str
    #: The user took over; the agent waits.
    paused: bool
    #: The session has no tab yet ("Watch" opens one).
    windowless: bool


class SessionActivity(GObject.Object):
    """Status of every open session by connection id. Emits "changed" on every update."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self) -> None:
        super().__init__()
        self.statuses: dict[str, SessionStatus] = {}
        self.agents: dict[str, AgentActivity] = {}

    def set_status(self, connection_id: str, status: SessionStatus | None) -> None:
        if status is None:
            self.statuses.pop(connection_id, None)
            self.agents.pop(connection_id, None)
        else:
            self.statuses[connection_id] = status
        self.emit("changed")

    def set_agent(self, connection_id: str, agent: AgentActivity | None) -> None:
        if agent is None:
            self.agents.pop(connection_id, None)
        else:
            self.agents[connection_id] = agent
        self.emit("changed")
