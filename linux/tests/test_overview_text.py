"""The overview's address and "last used" texts (as the macOS app's ConnectionRow shows them)."""

import unittest

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from weitblick.i18n import _, ngettext  # noqa: E402
from weitblick.store.connection import Connection, ConnectionProtocol, now_reference  # noqa: E402
from weitblick.ui.overview import address_text, last_used_text  # noqa: E402

DAY = 86400


class AddressTests(unittest.TestCase):
    def test_domain_user_and_standard_port(self):
        connection = Connection(name="Office PC", host="office-pc.example", username="anna", domain="OFFICE")
        self.assertEqual(address_text(connection), "OFFICE\\anna @ office-pc.example")

    def test_other_port_without_user(self):
        self.assertEqual(address_text(Connection(name="Server", host="server.example", port=3390)),
                         "server.example:3390")

    def test_screen_sharing(self):
        connection = Connection(name="Mac mini", host="mac-mini.example", port=5900, protocol=ConnectionProtocol.VNC)
        self.assertEqual(address_text(connection), "mac-mini.example · " + _("Screen Sharing"))


class LastUsedTests(unittest.TestCase):
    def last_used(self, seconds_ago):
        connection = Connection(name="PC", host="pc.example")
        connection.last_connected = None if seconds_ago is None else now_reference() - seconds_ago
        return last_used_text(connection)

    def test_steps(self):
        def text(unit, n):
            return ngettext(f"Last used {{n}} {unit} ago", f"Last used {{n}} {unit}s ago", n).format(n=n)

        self.assertEqual(self.last_used(None), _("Never connected"))
        self.assertEqual(self.last_used(10), _("Last used just now"))
        self.assertEqual(self.last_used(24 * 60), text("minute", 24))
        self.assertEqual(self.last_used(3 * 3600), text("hour", 3))
        self.assertEqual(self.last_used(DAY + 60), text("day", 1))
        self.assertEqual(self.last_used(15 * DAY), text("week", 2))
        self.assertEqual(self.last_used(35 * DAY), text("month", 1))
        self.assertEqual(self.last_used(400 * DAY), text("year", 1))

if __name__ == "__main__":
    unittest.main()
