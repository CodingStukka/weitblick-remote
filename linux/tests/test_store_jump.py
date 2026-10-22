"""Jump Desktop import (port of JumpImporterTests.swift)."""

import json
import tempfile
import unittest
from pathlib import Path

from weitblick.i18n import _
from weitblick.store import (AudioPlaybackMode, ConnectionProtocol, ConnectionStore, DriveMapping, ImportAction,
                             SecuritySettings, StaleImportPlan)
from weitblick.store.jump import JumpImporter, password_notice

#: Shared with the macOS app's tests, so both read the same Jump Desktop file.
SYNTHETIC_JUMP_FILE = (Path(__file__).resolve().parents[2] / "Packages" / "ConnectionStore" / "Tests" /
                       "ConnectionStoreTests" / "Fixtures" / "Synthetic.jump")


def synthetic_jump() -> dict:
    return json.loads(SYNTHETIC_JUMP_FILE.read_text())


def minimal_jump(unique_id="synthetic-1", host="PC0001.example", transport=0) -> dict:
    return {"UniqueId": unique_id, "DisplayName": "Synthetic workstation", "TcpHostName": host,
            "TcpPort": 3389 if transport == 0 else 5900, "ProtocolTypeCode": transport, "Username": "testuser"}


class TemporaryFolder:
    def __init__(self):
        self._handle = tempfile.TemporaryDirectory(prefix="weitblick-store-")
        self.path = Path(self._handle.name)

    @property
    def store_path(self) -> Path:
        return self.path / "connections.json"

    def write_jump(self, obj: dict, file_name="test.jump") -> None:
        (self.path / file_name).write_text(json.dumps(obj))

    def cleanup(self):
        self._handle.cleanup()


class JumpImporterTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryFolder()
        self.addCleanup(self.folder.cleanup)

    def importer(self):
        return JumpImporter(self.folder.path)

    def test_maps_every_spec_field(self):
        self.folder.write_jump(synthetic_jump())
        plan = self.importer().plan(existing=[])
        entry = plan.entries[0]
        c = entry.connection
        self.assertIs(entry.action, ImportAction.CREATE)
        self.assertEqual((c.name, c.host, c.port, c.username, c.domain),
                         ("Synthetic workstation", "PC0001.example", 13389, "testuser", "EXAMPLE"))
        self.assertIs(c.protocol, ConnectionProtocol.RDP)
        self.assertTrue(c.redirection.drive_redirection)
        self.assertEqual(c.redirection.drives, [
            DriveMapping(name="Test files", local_path="/Users/testuser/Downloads", read_only=True,
                         source_id="synthetic-drive-1"),
            DriveMapping(name="Work", local_path="/Users/testuser/Workspace", enabled=False,
                         source_id="synthetic-drive-2"),
        ])
        self.assertFalse(c.redirection.clipboard)
        self.assertIs(c.redirection.audio_playback, AudioPlaybackMode.LOCAL)
        self.assertTrue(c.redirection.microphone)
        self.assertEqual(c.redirection.audio_input_device, "Synthetic microphone")
        self.assertTrue(c.redirection.printers)
        self.assertEqual(c.redirection.default_printer, "Synthetic printer")
        d = c.display
        self.assertFalse(d.dynamic_resolution)
        self.assertFalse(d.match_screen_resolution)
        self.assertEqual((d.fixed_width, d.fixed_height), (1600, 900))
        self.assertTrue(d.retina)
        self.assertEqual(d.desktop_scale_factor, 150)
        self.assertTrue(d.start_fullscreen)
        self.assertTrue(d.use_all_monitors and d.monitor_count == 2)
        self.assertEqual(c.keyboard.layout_override, 1031)
        self.assertTrue(c.keyboard.unicode_text_input)
        s = c.security
        self.assertTrue(s.disable_nla and s.console_session and s.ignore_certificate_errors)
        self.assertEqual(s.trusted_certificate_fingerprints, ["SYNTHETIC:AA:BB:CC"])
        a = c.advanced
        self.assertEqual(a.alternate_shell, "C:\\Windows\\notepad.exe")
        self.assertEqual(a.working_dir, "C:\\Test")
        self.assertEqual(a.load_balance_info, "synthetic-routing-token")
        self.assertEqual(a.gateway_ref, "synthetic-gateway")
        self.assertEqual(a.wake_on_lan_mac_addresses, ["02:00:00:00:00:01"])
        self.assertEqual(c.tags, ["synthetic", "test"])
        self.assertEqual(c.last_connected, 600000000.125)
        self.assertEqual(c.import_source.unique_id, "synthetic-jump-0001")
        self.assertEqual(c.import_source.file_name, "test.jump")
        self.assertEqual(c.import_source.kind, "jump")
        report = plan.report.connections[0]
        self.assertTrue(any(i.field == "FutureJumpField" for i in report.ignored_fields))
        undocumented = _("Undocumented Jump code; not interpreted, the default stays.")
        for name in ("ColorDepthCode", "RdpPerformanceFlags", "ConnectionTypeCode", "TypeCode", "OsTypeCode",
                     "GestureProfileCode"):
            self.assertTrue(any(i.field == name and i.reason == undocumented for i in report.ignored_fields), name)
        self.assertIn(password_notice(), report.warnings)
        read_only = _("Drive “{name}” is read-only in Jump and is not shared (sharing needs write access).")
        self.assertIn(read_only.format(name="Test files"), report.warnings)
        self.assertNotIn(read_only.format(name="Work"), report.warnings)  # disabled: never shared anyway
        reported = set(report.imported_fields) | {i.field for i in report.ignored_fields}
        self.assertLessEqual(set(synthetic_jump()), reported)

    def test_idempotent_plan_apply_and_persistence(self):
        source = synthetic_jump()
        source["LastConnectedTime"] = 600000000.123456
        self.folder.write_jump(source)
        store = ConnectionStore(self.folder.store_path)
        first = self.importer().plan(existing=[])
        store.apply(first)
        reloaded = ConnectionStore(self.folder.store_path)
        existing = reloaded.load()
        again = self.importer().plan(existing=existing, imported_at=9e12)
        self.assertEqual((again.create_count, again.update_count, again.unchanged_count), (0, 0, 1))
        self.assertEqual(again.entries[0].connection.id, first.entries[0].connection.id)
        self.assertEqual(again.entries[0].connection.import_source.imported_at, existing[0].import_source.imported_at)
        source["DisplayName"] = "Updated synthetic workstation"
        self.folder.write_jump(source)
        update = self.importer().plan(existing=existing)
        self.assertEqual(update.update_count, 1)
        reloaded.apply(update)
        self.assertEqual(reloaded.connections[0].name, "Updated synthetic workstation")

    def test_stale_plan_rejected_and_caller_can_select_none(self):
        self.folder.write_jump(minimal_jump())
        plan = self.importer().plan(existing=[])
        store = ConnectionStore(self.folder.store_path)
        store.apply(plan, selected_ids=set())
        self.assertEqual(store.connections, [])
        store.apply(plan)
        with self.assertRaises(StaleImportPlan):
            store.apply(plan)
        changed = store.connections[0]
        source = minimal_jump()
        source["DisplayName"] = "Source change"
        self.folder.write_jump(source)
        update = self.importer().plan(existing=store.connections)
        changed.notes = "Concurrent local edit"
        store.update(changed)
        with self.assertRaises(StaleImportPlan):
            store.apply(update)
        self.assertEqual(store.connections[0].notes, "Concurrent local edit")

    def test_vnc_and_conservative_unknowns(self):
        source = minimal_jump(transport=1)
        source.update({"AudioPlaybackCode": 777, "KeyboardAutomaticLocaleDetection": True, "KeyboardLocaleId": 1033,
                       "DesktopScaleFactor": 0, "LastConnectedTime": 0, "AudioInputDevice": None, "Tags": None})
        self.folder.write_jump(source)
        plan = self.importer().plan(existing=[])
        c = plan.entries[0].connection
        self.assertIs(c.protocol, ConnectionProtocol.VNC)
        self.assertEqual(c.vnc_url, "vnc://PC0001.example:5900")
        self.assertIs(c.redirection.audio_playback, AudioPlaybackMode.OFF)
        self.assertFalse(c.redirection.microphone)
        self.assertIsNone(c.keyboard.layout_override)
        self.assertIsNone(c.display.desktop_scale_factor)
        self.assertIsNone(c.last_connected)
        self.assertTrue(any(i.field == "AudioPlaybackCode" for i in plan.report.connections[0].ignored_fields))
        source["ProtocolTypeCode"] = 7
        self.folder.write_jump(source)
        self.assertEqual(self.importer().plan(existing=[]).entries, [])

    def test_duplicate_ids_malformed_types_and_unreadable_files_reported(self):
        self.folder.write_jump(minimal_jump(), "a.jump")
        self.folder.write_jump(minimal_jump(), "b.jump")
        malformed = minimal_jump(unique_id="synthetic-other")
        malformed["TcpPort"] = "3389"
        malformed["ClipboardRedirection"] = 1
        malformed["DriveMappings"] = [{"DisplayName": "Synthetic", "LocalPath": "/tmp/synthetic", "Enabled": True,
                                       "UnknownDriveField": 5}]
        self.folder.write_jump(malformed, "c.jump")
        (self.folder.path / "d.jump").write_text("not JSON")
        self.folder.write_jump({"TcpHostName": "PC0003.example"}, "e.jump")
        plan = self.importer().plan(existing=[])
        self.assertEqual(len(plan.entries), 2)
        self.assertEqual(len(plan.report.connections), 5)
        self.assertIn(_("Duplicate UniqueId in the Jump folder; this file was not imported."),
                      plan.report.connections[1].warnings)
        issues = [i.field for i in plan.report.connections[2].ignored_fields]
        self.assertIn("TcpPort", issues)
        self.assertIn("ClipboardRedirection", issues)
        self.assertIn("DriveMappings[0].UnknownDriveField", issues)
        self.assertIsNone(plan.report.connections[3].action)
        self.assertIn(_("UniqueId missing; without it a later import couldn’t be matched. Not imported."),
                      plan.report.connections[4].warnings)
        self.assertIn("c.jump: Synthetic workstation [create]", plan.report.human_readable)

    def test_reimport_keeps_local_trust_and_newer_last_connected(self):
        source = minimal_jump()
        source["SslCertificateFingerPrint"] = "AA:BB:CC"
        source["LastConnectedTime"] = 600000000.0
        self.folder.write_jump(source)
        store = ConnectionStore(self.folder.store_path)
        store.apply(self.importer().plan(existing=[]))
        local = store.connections[0]
        local.security.trusted_certificate_fingerprints.append("dd:ee:ff")
        local.last_connected = 700000000.0
        store.update(local)
        again = self.importer().plan(existing=store.connections)
        self.assertEqual(again.unchanged_count, 1)
        self.assertEqual(again.entries[0].connection.security.trusted_certificate_fingerprints,
                         ["AA:BB:CC", "dd:ee:ff"])
        self.assertEqual(again.entries[0].connection.last_connected, 700000000.0)
        source["LastConnectedTime"] = 800000000.0
        source["SslCertificateFingerPrint"] = "11:22:33"
        self.folder.write_jump(source)
        update = self.importer().plan(existing=store.connections).entries[0].connection
        self.assertEqual(update.last_connected, 800000000.0)
        self.assertEqual(update.security.trusted_certificate_fingerprints, ["AA:BB:CC", "dd:ee:ff", "11:22:33"])

    def test_import_backs_up_the_existing_file_before_changing_it(self):
        self.folder.write_jump(minimal_jump())
        store = ConnectionStore(self.folder.store_path)
        store.apply(self.importer().plan(existing=[]))
        self.assertFalse(store.import_backup_path.exists())  # nothing to back up yet
        before = self.folder.store_path.read_bytes()
        source = minimal_jump()
        source["DisplayName"] = "Renamed in Jump"
        self.folder.write_jump(source)
        store.apply(self.importer().plan(existing=store.connections))
        self.assertEqual(store.import_backup_path.read_bytes(), before)
        self.assertEqual(store.connections[0].name, "Renamed in Jump")

    def test_fingerprint_comparison(self):
        security = SecuritySettings(trusted_certificate_fingerprints=["AB:CD:01"])
        self.assertTrue(security.trusts("ab cd 01"))

    def test_source_remains_unchanged(self):
        self.folder.write_jump(synthetic_jump())
        path = self.folder.path / "test.jump"
        before = path.read_bytes()
        self.importer().plan(existing=[])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.folder.path.iterdir()), ["test.jump"])


if __name__ == "__main__":
    unittest.main()
