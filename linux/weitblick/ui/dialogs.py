"""Sheets of a session: sign-in, certificate trust, disconnect confirmation (ports of SignInView,
CertificateView and the close alert)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from gi.repository import Adw, Gtk, Pango

from ..i18n import _
from ..rdp.session import CertificateDecision, ServerCertificate


@dataclass
class Credentials:
    """User name, domain and password for one connection attempt (saved or typed)."""

    username: str
    domain: str
    password: str


class SignInDialog(Adw.Dialog):
    """Asks for user name, domain and password; optionally saves the password in the keyring."""

    def __init__(self, connection_name: str, message: str | None, username: str, domain: str,
                 on_connect: Callable[[Credentials, bool], None]):
        super().__init__(title=_("Sign In"), content_width=420)
        self._on_connect = on_connect

        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self._connect_button = Gtk.Button(label=_("Connect"), css_classes=["suggested-action"])
        self._connect_button.connect("clicked", self._submit)
        header.pack_end(self._connect_button)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=_("Sign in to “{name}”").format(name=connection_name),
                                     description=message or "")
        self._user = Adw.EntryRow(title=_("User"), text=username)
        self._domain = Adw.EntryRow(title=_("Domain (optional)"), text=domain)
        self._password = Adw.PasswordEntryRow(title=_("Password"))
        self._save = Adw.SwitchRow(title=_("Save in keyring"), active=True)
        for row in (self._user, self._domain, self._password):
            row.connect("entry-activated", self._submit)
            row.connect("changed", lambda *_: self._validate())
            group.add(row)
        group.add(self._save)
        page.add(group)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(page)
        self.set_child(toolbar)
        self.set_default_widget(self._connect_button)
        self.set_focus(self._password if username else self._user)
        self._validate()

    def _validate(self) -> None:
        self._connect_button.set_sensitive(bool(self._user.get_text().strip()))

    def _submit(self, *_args) -> None:
        if not self._connect_button.get_sensitive():
            return
        credentials = Credentials(self._user.get_text().strip(), self._domain.get_text().strip(),
                                  self._password.get_text())
        save = self._save.get_active()
        self.close()
        self._on_connect(credentials, save)


def _grouped_fingerprint(fingerprint: str) -> str:
    """Colon-separated bytes in lines of eight, so the fingerprint is easy to compare."""
    parts = fingerprint.split(":")
    return "\n".join(":".join(parts[i:i + 8]) for i in range(0, len(parts), 8))


def present_certificate_dialog(parent: Gtk.Widget, certificate: ServerCertificate, connection_name: str,
                               on_decision: Callable[[CertificateDecision], None]) -> Adw.AlertDialog:
    """Trust on first use for a certificate without a valid chain, or a warning that it changed."""
    if certificate.changed:
        heading = _("The certificate of “{name}” has changed").format(name=connection_name)
        body = _("A different certificate is saved for this connection. That happens after a reinstall or a new "
                 "certificate, but it can also be an attack. Continue only if you know the reason.")
    else:
        heading = _("Unknown certificate from “{name}”").format(name=connection_name)
        body = _("The computer identifies itself with a self-signed certificate, which is common for Remote "
                 "Desktop. To be sure, compare the fingerprint with the one shown on the computer.")
    dialog = Adw.AlertDialog(heading=heading, body=body)

    grid = Gtk.Grid(column_spacing=12, row_spacing=6, css_classes=["card"], margin_top=6)
    grid.set_margin_start(0)
    rows = [
        (_("Host"), f"{certificate.host}:{certificate.port}", False),
        (_("Issued to"), certificate.common_name or certificate.subject, False),
        (_("Issuer"), certificate.issuer, False),
        (_("SHA-256"), _grouped_fingerprint(certificate.fingerprint), True),
    ]
    for index, (label, value, mono) in enumerate(rows):
        key = Gtk.Label(label=label, xalign=1, yalign=0, css_classes=["dim-label"], margin_start=12)
        text = Gtk.Label(label=value or "–", xalign=0, selectable=True, wrap=True,
                         wrap_mode=Pango.WrapMode.WORD_CHAR, margin_end=12)
        if mono:
            text.add_css_class("monospace")
        if index == 0:
            key.set_margin_top(10)
            text.set_margin_top(10)
        if index == len(rows) - 1:
            key.set_margin_bottom(10)
            text.set_margin_bottom(10)
        grid.attach(key, 0, index, 1, 1)
        grid.attach(text, 1, index, 1, 1)
    extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    extra.append(grid)
    if certificate.hostname_mismatch:
        warning = Gtk.Label(label=_("The certificate names a different computer than “{host}”.").format(
            host=certificate.host), wrap=True, xalign=0, css_classes=["warning"])
        extra.append(warning)
    dialog.set_extra_child(extra)

    dialog.add_response("reject", _("Cancel"))
    dialog.add_response("once", _("Connect Once"))
    dialog.add_response("always", _("Trust New Certificate") if certificate.changed else _("Always Trust"))
    dialog.set_response_appearance("always", Adw.ResponseAppearance.DESTRUCTIVE if certificate.changed
                                   else Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("reject" if certificate.changed else "always")
    dialog.set_close_response("reject")
    decisions = {"reject": CertificateDecision.REJECT, "once": CertificateDecision.ACCEPT_ONCE,
                 "always": CertificateDecision.ACCEPT_PERMANENTLY}
    dialog.connect("response", lambda _d, response: on_decision(decisions.get(response, CertificateDecision.REJECT)))
    dialog.present(parent)
    return dialog


def confirm_disconnect(parent: Gtk.Widget, connection_name: str, agent_session: bool,
                       on_answer: Callable[[bool, bool], None]) -> None:
    """Asks before closing a session tab; on_answer(disconnect, dont_ask_again)."""
    dialog = Adw.AlertDialog(
        heading=_("Disconnect from “{name}”?").format(name=connection_name),
        body=_("The agent loses this session. Open programs keep running in Windows.") if agent_session
        else _("The tab closes. Open programs keep running in Windows."))
    check = Gtk.CheckButton(label=_("Don’t ask again"))
    dialog.set_extra_child(check)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("disconnect", _("Disconnect"))
    dialog.set_response_appearance("disconnect", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("disconnect")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, response: on_answer(response == "disconnect", check.get_active()))
    dialog.present(parent)


def show_message(parent: Gtk.Widget, heading: str, body: str) -> None:
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("ok", _("OK"))
    dialog.present(parent)
