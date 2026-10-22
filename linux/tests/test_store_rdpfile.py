""".rdp import and export (port of RDPFileTests.swift)."""

import unittest

from weitblick.i18n import _
from weitblick.store import AudioPlaybackMode, Connection, ConnectionProtocol
from weitblick.store import rdpfile
from weitblick.store.rdpfile import RDPFileEncoding, RDPFileError

from tests.test_store_jump import TemporaryFolder


class RDPFileTests(unittest.TestCase):
    def test_round_trip_all_supported_fields(self):
        for encoding in RDPFileEncoding:
            with self.subTest(encoding=encoding):
                c = Connection(name="Synthetic", host="PC0001.example", port=13389, username="testuser",
                               domain="EXAMPLE")
                c.display.fixed_width, c.display.fixed_height = 1600, 900
                c.display.start_fullscreen = True
                c.display.dynamic_resolution = False
                c.display.use_all_monitors = True
                c.redirection.clipboard = False
                c.redirection.audio_playback = AudioPlaybackMode.REMOTE
                c.redirection.microphone = True
                c.redirection.printers = False
                c.advanced.rdp_drive_store_direct = "C:;D:;"
                c.advanced.alternate_shell = "C:\\Windows\\notepad.exe"
                c.advanced.working_dir = "C:\\Test"
                c.advanced.load_balance_info = "synthetic:token"
                c.advanced.gateway_hostname = "gateway.example"
                c.security.authentication_level = 1
                c.security.prompt_for_credentials = False
                data = rdpfile.encode(c, encoding)
                if encoding is RDPFileEncoding.UTF16LE:
                    self.assertTrue(data.startswith(b"\xff\xfe"))
                decoded = rdpfile.decode(data, name=c.name).connection
                c.id = decoded.id
                self.assertEqual(decoded, c)

    def test_audio_code_space(self):
        for mode in AudioPlaybackMode:
            c = Connection(name="Audio", host="PC0001.example")
            c.redirection.audio_playback = mode
            self.assertIs(rdpfile.decode(rdpfile.encode(c)).connection.redirection.audio_playback, mode)

    def test_ipv6_ports_utf8_bom_and_read_file(self):
        folder = TemporaryFolder()
        self.addCleanup(folder.cleanup)
        path = folder.path / "Synthetic.rdp"
        path.write_bytes(b"\xef\xbb\xbf" + "full address:s:[2001:db8::1]:3390\r\nusername:s:testuser\r\n".encode())
        imported = rdpfile.read(path)
        self.assertEqual((imported.connection.host, imported.connection.port), ("2001:db8::1", 3390))
        self.assertEqual(imported.connection.name, "Synthetic")
        round_trip = rdpfile.decode(rdpfile.encode(imported.connection))
        self.assertEqual(round_trip.connection.host, "2001:db8::1")
        self.assertEqual(rdpfile.decode(b"full address:s:PC0001.example\n").connection.port, 3389)
        vnc = imported.connection.copy()
        vnc.protocol = ConnectionProtocol.VNC
        self.assertEqual(vnc.vnc_url, "vnc://[2001:db8::1]:3390")

    def test_unknowns_duplicates_and_secrets_are_reported_without_values(self):
        text = "\n".join([
            "full address:s:PC0001.example:3389",
            "username:s:firstuser",
            "username:s:testuser",
            "password 51:b:SYNTHETIC-SECRET-DO-NOT-REPORT",
            "unknownfield:i:5",
            "malformed-line",
            "drivestoredirect:s:*",
            "authentication level:i:0",
        ])
        imported = rdpfile.decode(text.encode())
        self.assertEqual(imported.connection.username, "testuser")
        self.assertTrue(imported.connection.security.ignore_certificate_errors)
        self.assertFalse(imported.connection.redirection.drive_redirection)
        self.assertEqual(imported.connection.redirection.drives, [])
        self.assertEqual([issue.field for issue in imported.ignored_fields], ["unknownfield"])
        self.assertIn(_("Saved password in the file ignored; you’ll be asked for it when connecting."),
                      imported.warnings)
        self.assertNotIn("SYNTHETIC-SECRET", "".join(imported.warnings))
        encoded = rdpfile.encode(imported.connection, RDPFileEncoding.UTF8)
        self.assertNotIn(b"password", encoded)

    def test_invalid_address_types_and_line_injection_rejected(self):
        for text in ("full address:s:PC0001.example:0", "full address:s:[bad", "full address:i:3389",
                     "full address:s:PC0001.example\naudiomode:i:9",
                     "full address:s:PC0001.example\nredirectclipboard:i:2"):
            with self.assertRaises(RDPFileError, msg=text):
                rdpfile.decode(text.encode())
        with self.assertRaises(RDPFileError) as raised:
            rdpfile.decode(b"username:s:testuser")
        self.assertEqual(raised.exception, RDPFileError("missingAddress"))
        with self.assertRaises(RDPFileError) as raised:
            rdpfile.decode(bytes([0xFF, 0xAB, 0xFE]))
        self.assertEqual(raised.exception, RDPFileError("invalidEncoding"))
        c = Connection(name="Test", host="PC0001.example")
        c.username = "testuser\nredirectclipboard:i:1"
        with self.assertRaises(RDPFileError) as raised:
            rdpfile.encode(c)
        self.assertEqual(raised.exception, RDPFileError("invalidValue", "username"))
        c.protocol = ConnectionProtocol.VNC
        with self.assertRaises(RDPFileError) as raised:
            rdpfile.encode(c)
        self.assertEqual(raised.exception, RDPFileError("unsupportedProtocol"))


if __name__ == "__main__":
    unittest.main()
