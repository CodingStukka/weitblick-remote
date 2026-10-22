"""What the connection editor works on (port of ConnectionDraft.swift): a copy of the connection
plus the password field. Nothing is stored until the editor saves `saved()`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..i18n import _
from .connection import Connection


@dataclass(frozen=True)
class PasswordChange:
    """kind: "keep", "set" (with the password) or "remove"."""

    kind: str
    password: str = ""


KEEP_PASSWORD = PasswordChange("keep")
REMOVE_PASSWORD = PasswordChange("remove")


@dataclass
class ConnectionDraft:
    connection: Connection
    is_new: bool
    #: A password is saved for this connection (the field then stays empty: "unchanged").
    has_stored_password: bool
    #: Typed password; empty keeps the stored one.
    password: str = ""
    remove_stored_password: bool = False

    @classmethod
    def new(cls) -> ConnectionDraft:
        return cls(connection=Connection(name="", host=""), is_new=True, has_stored_password=False)

    @classmethod
    def editing(cls, connection: Connection, has_stored_password: bool) -> ConnectionDraft:
        return cls(connection=connection.copy(), is_new=False, has_stored_password=has_stored_password)

    @property
    def problem(self) -> str | None:
        """Why the draft can't be saved yet; None when it can."""
        host = self.connection.host.strip()
        if not host:
            return _("Enter a host.")
        if any(ch.isspace() for ch in host):
            return _("The host can’t contain spaces.")
        if not 1 <= self.connection.port <= 65535:
            return _("The port must be between 1 and 65535.")
        display = self.connection.display
        if not display.match_screen_resolution and (
                not 200 <= display.fixed_width <= 8192 or not 200 <= display.fixed_height <= 8192):
            return _("The fixed resolution must be between 200 and 8192 pixels.")
        return None

    @property
    def password_change(self) -> PasswordChange:
        if self.password:
            return PasswordChange("set", self.password)
        return REMOVE_PASSWORD if self.remove_stored_password else KEEP_PASSWORD

    def saved(self) -> Connection:
        """The connection to store: trimmed text, the host as name when the name is empty."""
        result = self.connection.copy()
        result.host = result.host.strip()
        result.name = result.name.strip() or result.host
        result.username = result.username.strip()
        result.domain = result.domain.strip()
        advanced = result.advanced
        advanced.wake_on_lan_mac_addresses = [text.strip(" \t") for text in advanced.wake_on_lan_mac_addresses
                                              if text.strip(" \t")]
        for name in ("alternate_shell", "working_dir", "load_balance_info"):
            value = getattr(advanced, name)
            if value is not None and not value.strip(" \t"):
                setattr(advanced, name, None)
        return result

