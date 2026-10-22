"""Remmina import."""

import os
import unittest
from unittest import mock

from weitblick.i18n import _
from weitblick.store import (AudioPlaybackMode, ConnectionProtocol, ConnectionStore, DriveMapping, ImportAction,
                             StaleImportPlan)
from weitblick.store.remmina import RemminaImporter, default_directories, parse_server, password_notice

from tests.test_store_jump import TemporaryFolder

RDP_FILE = """[remmina]
name=Lab PC
server=lab01.example:13389
protocol=RDP
username=EXAMPLE\\testuser
domain=
group=Customers/Lab
password=c29tZXRoaW5nIGVuY3J5cHRlZA==
resolution_mode=0
resolution_width=1600
resolution_height=900
scale=2
viewmode=4
sharefolder=/home/testuser/Shared
disableclipboard=1
sound=remote
microphone=0
shareprinter=1
cert_ignore=1
security=tls
console=1
exec=C:\\Windows\\notepad.exe
execpath=C:\\Test
loadbalanceinfo=tsv://MS Terminal Services Plugin.1.Farm
gateway_server=
colordepth=99
quality=0
window_width=1280
"""

VNC_FILE = """[remmina]
name=Mini
server=m4-mini
protocol=VNC
"""


class RemminaImporterTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryFolder()
        self.addCleanup(self.folder.cleanup)

    def write(self, name, text):
        (self.folder.path / name).write_text(text)

    def plan(self, existing=()):
        return RemminaImporter(self.folder.path).plan(existing=list(existing))

    def test_maps_rdp_settings(self):
        self.write("1600000000000.remmina", RDP_FILE)
        plan = self.plan()
        entry = plan.entries[0]
        c = entry.connection
        self.assertIs(entry.action, ImportAction.CREATE)
        self.assertEqual((c.name, c.host, c.port), ("Lab PC", "lab01.example", 13389))
        self.assertEqual((c.username, c.domain), ("testuser", "EXAMPLE"))
        self.assertIs(c.protocol, ConnectionProtocol.RDP)
        self.assertEqual(c.tags, ["Customers", "Lab"])
        self.assertFalse(c.display.match_screen_resolution)
        self.assertEqual((c.display.fixed_width, c.display.fixed_height), (1600, 900))
        self.assertTrue(c.display.dynamic_resolution)
        self.assertTrue(c.display.start_fullscreen)
        self.assertTrue(c.redirection.drive_redirection)
        self.assertEqual(c.redirection.drives, [DriveMapping(name="Shared", local_path="/home/testuser/Shared")])
        self.assertFalse(c.redirection.clipboard)
        self.assertIs(c.redirection.audio_playback, AudioPlaybackMode.REMOTE)
        self.assertFalse(c.redirection.microphone)
        self.assertTrue(c.redirection.printers)
        self.assertTrue(c.security.ignore_certificate_errors)
        self.assertTrue(c.security.disable_nla)
        self.assertTrue(c.security.console_session)
        self.assertEqual(c.advanced.alternate_shell, "C:\\Windows\\notepad.exe")
        self.assertEqual(c.advanced.working_dir, "C:\\Test")
        self.assertEqual(c.advanced.load_balance_info, "tsv://MS Terminal Services Plugin.1.Farm")
        self.assertIsNone(c.advanced.gateway_hostname)
        self.assertEqual((c.import_source.kind, c.import_source.unique_id), ("remmina", "1600000000000.remmina"))
        report = plan.report.connections[0]
        self.assertIn(password_notice(), report.warnings)
        ignored = {issue.field: issue.reason for issue in report.ignored_fields}
        self.assertEqual(ignored["password"], _("Saved password; not imported."))
        for key in ("colordepth", "window_width"):
            self.assertEqual(ignored[key], _("No equivalent; not imported."))
        self.assertNotIn("quality", ignored)  # 0: an off switch, nothing to take over
        self.assertNotIn("gateway_server", ignored)  # empty values mean "not set"
        self.assertNotIn("password", report.imported_fields)
        self.assertNotIn("c29tZXRoaW5n", plan.report.human_readable)
        self.assertTrue(plan.report.human_readable.startswith("Remmina import"))

    def test_vnc_kept_other_protocols_skipped(self):
        self.write("a.remmina", VNC_FILE)
        self.write("b.remmina", "[remmina]\nname=Shell\nserver=box\nprotocol=SSH\n")
        self.write("c.remmina", "not an ini file")
        self.write("d.txt", RDP_FILE)
        plan = self.plan()
        self.assertEqual(len(plan.entries), 1)
        vnc = plan.entries[0].connection
        self.assertIs(vnc.protocol, ConnectionProtocol.VNC)
        self.assertEqual(vnc.vnc_url, "vnc://m4-mini:5900")
        self.assertEqual(len(plan.report.connections), 3)
        self.assertIsNone(plan.report.connections[1].action)
        self.assertIn(_("Only RDP and VNC connections are imported; this one is not."),
                      plan.report.connections[1].warnings)
        self.assertIn(_("Not a readable Remmina file; nothing planned."), plan.report.connections[2].warnings)

    def test_client_resolution_and_sound_options(self):
        self.write("x.remmina", "[remmina]\nserver=[2001:db8::1]:3390\nprotocol=RDP\nresolution_mode=1\n"
                                "sound=local,quality:high\nmicrophone=sys:pulse\nscale=0\nsecurity=nla\n")
        c = self.plan().entries[0].connection
        self.assertEqual((c.host, c.port, c.name), ("2001:db8::1", 3390, "2001:db8::1"))
        self.assertTrue(c.display.match_screen_resolution)
        self.assertFalse(c.display.dynamic_resolution)
        self.assertIs(c.redirection.audio_playback, AudioPlaybackMode.LOCAL)
        self.assertTrue(c.redirection.microphone)
        self.assertFalse(c.security.disable_nla)

    def test_idempotent_reimport_and_update(self):
        self.write("lab.remmina", RDP_FILE)
        store = ConnectionStore(self.folder.store_path)
        first = self.plan()
        store.apply(first)
        again = self.plan(store.connections)
        self.assertEqual((again.create_count, again.update_count, again.unchanged_count), (0, 0, 1))
        self.assertEqual(again.entries[0].connection.id, first.entries[0].connection.id)
        with self.assertRaises(StaleImportPlan):
            store.apply(first)
        self.write("lab.remmina", RDP_FILE.replace("name=Lab PC", "name=Lab PC 2"))
        update = self.plan(store.connections)
        self.assertEqual(update.update_count, 1)
        store.apply(update)
        self.assertEqual(store.connections[0].name, "Lab PC 2")

    def test_parse_server(self):
        self.assertEqual(parse_server("pc01", 3389), ("pc01", 3389))
        self.assertEqual(parse_server(" pc01:3390 ", 3389), ("pc01", 3390))
        self.assertEqual(parse_server("2001:db8::1", 3389), ("2001:db8::1", 3389))
        for bad in ("", "pc01:0", "pc01:x", "[::1", "pc 01"):
            self.assertIsNone(parse_server(bad, 3389), bad)


class RemminaFolderTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryFolder()
        self.addCleanup(self.folder.cleanup)
        self.desktop = self.folder.path / "desktop-data"
        self.sandbox = self.folder.path / "sandbox-data"
        for base in (self.desktop, self.sandbox):
            (base / "remmina").mkdir(parents=True)

    def test_uses_the_data_folder(self):
        with mock.patch.dict(os.environ, {"FLATPAK_ID": "", "XDG_DATA_HOME": str(self.desktop),
                                          "HOST_XDG_DATA_HOME": str(self.sandbox)}):
            self.assertEqual(default_directories()[0], self.desktop / "remmina")

    def test_in_the_flatpak_uses_the_desktops_data_folder(self):
        with mock.patch.dict(os.environ, {"FLATPAK_ID": "nrw.neuhaus.WeitblickRemote",
                                          "XDG_DATA_HOME": str(self.sandbox), "HOST_XDG_DATA_HOME": str(self.desktop)}):
            self.assertEqual(default_directories()[0], self.desktop / "remmina")


if __name__ == "__main__":
    unittest.main()
