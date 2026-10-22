"""The per-connection permission gate for AI agents: policy, prompts and grants. Port of
AccessGate.swift for a GLib main loop app: instead of async/await, `check` reports its decision
through a callback (possibly right away), and prompt timeouts run on an injectable scheduler (GLib
timeouts by default). Main loop only. The clock, the time zone (midnight) and the durations are
injectable so tests control time.

The app drives it like this:

    gate = AccessGate()
    gate.show_prompt = lambda prompt: panel.show(prompt)          # answer later with gate.answer()
    gate.close_prompt = lambda prompt: panel.close(prompt)
    gate.persist_always_allow = lambda connection_id: store.set_agent_access(connection_id, "always")
    gate.on_change = settings_page.refresh                         # grants or prompts changed
    gate.check(AccessRequest(...), AgentAccessPolicy.ASK, lambda decision: ...)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from enum import Enum
from typing import Callable

from .errors import format_number


class AgentAccessPolicy(Enum):
    """A connection's "AI agent access" setting (Connection.security.agentAccess)."""

    ASK = "ask"
    ALWAYS = "always"
    NEVER = "never"


class PromptAnswer(Enum):
    """The user's answer in the permission prompt."""

    #: This request only.
    DENY = "deny"
    #: The session the request is for, until it disconnects.
    ALLOW_ONCE = "once"
    #: Every connect and tool call on the connection for timed_grant_duration (10 minutes).
    ALLOW_TEN_MINUTES = "ten-minutes"
    #: Every connect and tool call on the connection until local midnight.
    ALLOW_TODAY = "today"
    #: Switches the connection to "Always allow" (persisted by the app).
    ALWAYS_ALLOW = "always"


@dataclass(frozen=True)
class AccessRequest:
    """What an agent wants: a session (new or existing) on a connection."""

    connection_id: str
    connection_name: str
    host: str
    #: MCP client display name ("claude-code 2.1.3").
    client: str
    #: The session the request is for; for connect the id the new session will get.
    session_id: str


@dataclass(frozen=True)
class AccessPrompt:
    """An open permission prompt."""

    id: int
    request: AccessRequest
    #: When it opened (seconds since the epoch).
    opened: float


@dataclass(frozen=True)
class AccessGrant:
    """A permission the user gave in a prompt (kept in memory only)."""

    id: int
    connection_id: str
    connection_name: str
    client: str
    #: "Allow once": this session until it disconnects (else None).
    session_id: str | None
    #: "Allow for 10 minutes" / "Allow today": the whole connection until then (epoch seconds, else None).
    until: float | None
    granted: float


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    #: The message for the model when refused.
    message: str | None = None

    @staticmethod
    def refused(message: str) -> AccessDecision:
        return AccessDecision(False, message)


ALLOWED = AccessDecision(True)


def _glib_schedule(seconds: float, callback: Callable[[], None]) -> Callable[[], None]:
    """Runs `callback` once after `seconds` on the GLib main loop; returns a cancel function."""
    from gi.repository import GLib

    def fire() -> bool:
        callback()
        return GLib.SOURCE_REMOVE

    source = GLib.timeout_add(max(0, int(seconds * 1000)), fire)

    def cancel() -> None:
        if GLib.MainContext.default().find_source_by_id(source):
            GLib.source_remove(source)
    return cancel


class _Pending:
    def __init__(self, prompt: AccessPrompt, waiter: Callable):
        self.prompt = prompt
        self.waiters: list[Callable] = [waiter]
        self.cancel_timeout: Callable[[], None] | None = None


# Outcomes of a prompt, handed to its waiters.
_ANSWERED, _TIMED_OUT, _CANCELLED = "answered", "timed-out", "cancelled"


class AccessGate:
    def __init__(self, now: Callable[[], float] = time.time, timezone: tzinfo | None = None,
                 prompt_timeout: float = 120.0, timed_grant_duration: float = 600.0,
                 schedule: Callable[[float, Callable[[], None]], Callable[[], None]] = _glib_schedule):
        self.now = now
        #: For "today" (local midnight); None = the system's local time zone.
        self.timezone = timezone
        #: How long a check waits for an answer (seconds).
        self.prompt_timeout = prompt_timeout
        #: "Allow for 10 minutes" (seconds).
        self.timed_grant_duration = timed_grant_duration
        self.schedule = schedule

        #: Shows a prompt. It may answer right away (answer()), also from inside this call.
        self.show_prompt: Callable[[AccessPrompt], None] = lambda prompt: None
        #: The prompt got answered or timed out: close its panel.
        self.close_prompt: Callable[[AccessPrompt], None] = lambda prompt: None
        #: "Always allow": persist the policy for the connection id.
        self.persist_always_allow: Callable[[str], None] = lambda connection_id: None
        #: Grants or prompts changed (settings list).
        self.on_change: Callable[[], None] = lambda: None

        self._grants: list[AccessGrant] = []
        self._pending: dict[str, _Pending] = {}  # by connection id
        self._next_id = 1

    # MARK: - Checking

    def check(self, request: AccessRequest, policy: AgentAccessPolicy,
              done: Callable[[AccessDecision], None]) -> None:
        """Decides a request and calls `done` exactly once, possibly before returning. For "Ask"
        without a grant it opens (or joins) the connection's prompt and waits for the answer. Call it
        for every connect and every tool call on a session."""
        if policy is AgentAccessPolicy.NEVER:
            done(AccessDecision.refused(self.never_message(request.connection_name)))
            return
        if policy is AgentAccessPolicy.ALWAYS or self.is_granted(request):
            done(ALLOWED)
            return

        def on_outcome(kind: str, answer: PromptAnswer | None, asked: AccessRequest | None, message: str | None):
            if kind == _ANSWERED:
                if answer is PromptAnswer.DENY:
                    done(AccessDecision.refused(f"The user denied access to \"{request.connection_name}\"."))
                elif answer is PromptAnswer.ALLOW_ONCE and asked.session_id != request.session_id:
                    # Granted for the session that was asked about; another session asks for itself.
                    self.check(request, policy, done)
                else:
                    done(ALLOWED)
            elif kind == _TIMED_OUT:
                done(AccessDecision.refused(
                    f"No answer from the user: the permission prompt for \"{request.connection_name}\" was open "
                    f"for {self.describe(self.prompt_timeout)}. Ask the user to allow it and try again."))
            else:
                done(AccessDecision.refused(message or "The request was cancelled."))

        self._wait_for_outcome(request, on_outcome)

    def is_granted(self, request: AccessRequest) -> bool:
        """Whether a grant covers the request right now (no prompt). Expired grants are dropped."""
        self._remove_expired()
        return any(grant.connection_id == request.connection_id
                   and (grant.session_id is None or grant.session_id == request.session_id)
                   for grant in self._grants)

    def _wait_for_outcome(self, request: AccessRequest, waiter: Callable) -> None:
        pending = self._pending.get(request.connection_id)
        if pending is not None:
            pending.waiters.append(waiter)
            return
        prompt = AccessPrompt(self._next_id, request, self.now())
        self._next_id += 1
        pending = _Pending(prompt, waiter)
        self._pending[request.connection_id] = pending
        pending.cancel_timeout = self.schedule(
            self.prompt_timeout, lambda: self._resolve(prompt.id, _TIMED_OUT, None, None, None))
        self.on_change()
        self.show_prompt(prompt)

    # MARK: - Answers

    def answer(self, prompt_id: int, answer: PromptAnswer) -> None:
        """The user's (or a test hook's) answer to an open prompt. Unknown ids are ignored."""
        pending = next((entry for entry in self._pending.values() if entry.prompt.id == prompt_id), None)
        if pending is None:
            return
        request = pending.prompt.request
        date = self.now()
        if answer is PromptAnswer.ALLOW_ONCE:
            self._add_grant(request, session_id=request.session_id, until=None, at=date)
        elif answer is PromptAnswer.ALLOW_TEN_MINUTES:
            self._add_grant(request, session_id=None, until=date + self.timed_grant_duration, at=date)
        elif answer is PromptAnswer.ALLOW_TODAY:
            self._add_grant(request, session_id=None, until=self.next_midnight(date), at=date)
        elif answer is PromptAnswer.ALWAYS_ALLOW:
            self.persist_always_allow(request.connection_id)
        self._resolve(prompt_id, _ANSWERED, answer, request, None)

    def cancel_all_prompts(self, message: str) -> None:
        """Refuses every open prompt (master switch turned off, app quitting)."""
        for pending in list(self._pending.values()):
            self._resolve(pending.prompt.id, _CANCELLED, None, None, message)

    @property
    def open_prompts(self) -> list[AccessPrompt]:
        return sorted((entry.prompt for entry in self._pending.values()), key=lambda prompt: prompt.id)

    def _resolve(self, prompt_id: int, kind: str, answer: PromptAnswer | None, asked: AccessRequest | None,
                 message: str | None) -> None:
        key = next((key for key, entry in self._pending.items() if entry.prompt.id == prompt_id), None)
        if key is None:
            return
        pending = self._pending.pop(key)
        if pending.cancel_timeout is not None and kind != _TIMED_OUT:
            pending.cancel_timeout()
        self.close_prompt(pending.prompt)
        self.on_change()
        for waiter in pending.waiters:
            waiter(kind, answer, asked, message)

    # MARK: - Grants

    @property
    def active_grants(self) -> list[AccessGrant]:
        """Active grants, oldest first."""
        self._remove_expired()
        return list(self._grants)

    def revoke(self, grant_id: int) -> None:
        count = len(self._grants)
        self._grants = [grant for grant in self._grants if grant.id != grant_id]
        if len(self._grants) != count:
            self.on_change()

    def session_ended(self, session_id: str) -> None:
        """A session ended: its "Allow once" grant goes with it."""
        count = len(self._grants)
        self._grants = [grant for grant in self._grants if grant.session_id != session_id]
        if len(self._grants) != count:
            self.on_change()

    def revoke_all(self) -> None:
        """Forgets everything (master switch off)."""
        if self._grants:
            self._grants = []
            self.on_change()

    def next_midnight(self, after: float) -> float:
        """The first local midnight after `after` (epoch seconds), daylight saving included."""
        if self.timezone is None:
            next_day = datetime.fromtimestamp(after).date() + timedelta(days=1)
            # Naive: interpreted in the system's local time zone.
            return datetime(next_day.year, next_day.month, next_day.day).timestamp()
        next_day = datetime.fromtimestamp(after, self.timezone).date() + timedelta(days=1)
        return datetime(next_day.year, next_day.month, next_day.day, tzinfo=self.timezone).timestamp()

    def _add_grant(self, request: AccessRequest, session_id: str | None, until: float | None, at: float) -> None:
        self._grants.append(AccessGrant(self._next_id, request.connection_id, request.connection_name,
                                        request.client, session_id, until, at))
        self._next_id += 1
        self.on_change()

    def _remove_expired(self) -> None:
        date = self.now()
        count = len(self._grants)
        self._grants = [grant for grant in self._grants if grant.until is None or grant.until > date]
        if len(self._grants) != count:
            self.on_change()

    # MARK: - Messages (for the model: English)

    @staticmethod
    def never_message(connection_name: str) -> str:
        return (f"AI agent access is set to Never for \"{connection_name}\". The user can change it in Weitblick "
                "Remote: edit the connection, Security, AI agent access.")

    @staticmethod
    def describe(seconds: float) -> str:
        if seconds >= 60 and seconds % 60 == 0:
            minutes = int(seconds // 60)
            return "1 minute" if minutes == 1 else f"{minutes} minutes"
        return "1 second" if seconds == 1 else f"{format_number(seconds)} seconds"
