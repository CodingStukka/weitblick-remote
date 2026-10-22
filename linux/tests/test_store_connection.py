"""The connection model and its JSON form (compatibility with the macOS app's connections.json)."""

import json
import unittest
from datetime import datetime, timezone

from weitblick.store import (AgentAccess, AudioPlaybackMode, Connection, ConnectionProtocol, DriveMapping,
                             InvalidConnection, InvalidStore, KeyboardSettings, SecuritySettings, TextInput,
                             UnsupportedSchema, datetime_to_reference, reference_to_datetime)

# A record as the Mac app writes it (Swift JSONEncoder, sorted keys), including Mac-only keyboard keys
# and a key a newer version might add.
MAC_RECORD = {
    "advanced": {"alternateShell": "C:\\Windows\\notepad.exe", "wakeOnLANMACAddresses": ["02:00:00:00:00:01"]},
    "display": {"dynamicResolution": True, "fixedHeight": 768, "fixedWidth": 1024, "matchScreenResolution": True,
                "monitorCount": 1, "retina": True, "startFullscreen": False, "useAllMonitors": False},
    "domain": "EXAMPLE",
    "futureTopLevel": {"kept": [1, 2]},
    "host": "PC0001.example",
    "id": "0f8fad5b-d9cb-469f-a165-70867728950e",
    "importSource": {"fileName": "test.jump", "importedAt": 600000000.5, "kind": "jump",
                     "uniqueID": "synthetic-1"},
    "keyboard": {"layoutOverride": "00000407", "mode": "macShortcuts", "modifierTapTimeout": 0.5,
                 "optionStrategy": "smart", "reservedShortcuts": ["ctrl+cmd+f"],
                 "rules": [{"from": "cmd+q", "to": "alt+f4"}], "unicodeTextInput": False},
    "lastConnected": 600000000.123456,
    "name": "Synthetic",
    "notes": "",
    "port": 3389,
    "protocol": "rdp",
    "redirection": {"audioPlayback": "remote", "clipboard": True, "driveRedirection": True,
                    "drives": [{"enabled": True, "localPath": "~/Downloads", "name": "Downloads",
                                "readOnly": False}],
                    "microphone": False, "printers": True},
    "schemaVersion": 1,
    "security": {"agentAccess": "never", "authenticationLevel": 2, "consoleSession": False, "disableNLA": False,
                 "ignoreCertificateErrors": False, "promptForCredentials": True,
                 "trustedCertificateFingerprints": ["AA:BB"]},
    "tags": ["lab"],
    "username": "testuser",
}


class ConnectionJSONTests(unittest.TestCase):
    def test_mac_record_decodes(self):
        c = Connection.from_json(MAC_RECORD)
        self.assertEqual(c.id, "0F8FAD5B-D9CB-469F-A165-70867728950E")
        self.assertEqual((c.name, c.host, c.port, c.username, c.domain), ("Synthetic", "PC0001.example", 3389,
                                                                          "testuser", "EXAMPLE"))
        self.assertIs(c.protocol, ConnectionProtocol.RDP)
        self.assertTrue(c.display.retina)
        self.assertIsNone(c.display.desktop_scale_factor)
        self.assertIs(c.redirection.audio_playback, AudioPlaybackMode.REMOTE)
        self.assertEqual(c.redirection.drives, [DriveMapping(name="Downloads", local_path="~/Downloads")])
        self.assertEqual(c.keyboard.layout_override, 0x0407)
        self.assertIs(c.keyboard.text_input, TextInput.SMART)
        self.assertIs(c.security.agent_access, AgentAccess.NEVER)
        self.assertEqual(c.last_connected, 600000000.123456)
        self.assertEqual(c.import_source.unique_id, "synthetic-1")
        self.assertEqual(c.advanced.alternate_shell, "C:\\Windows\\notepad.exe")

    def test_round_trip_keeps_unknown_and_mac_only_keys(self):
        encoded = Connection.from_json(MAC_RECORD).to_json()
        expected = json.loads(json.dumps(MAC_RECORD))
        expected["id"] = expected["id"].upper()
        expected["keyboard"]["textInput"] = "smart"  # the Linux key is added, the Mac ones stay
        self.assertEqual(encoded, expected)

    def test_new_connection_omits_unset_optionals_and_uses_uppercase_ids(self):
        encoded = Connection(name="PC", host="pc01").to_json()
        self.assertEqual(encoded["id"], encoded["id"].upper())
        for key in ("lastConnected", "importSource"):
            self.assertNotIn(key, encoded)
        self.assertNotIn("desktopScaleFactor", encoded["display"])
        self.assertNotIn("alternateShell", encoded["advanced"])
        self.assertNotIn("layoutOverride", encoded["keyboard"])
        # Every non-optional key the Mac app's synthesized decoder requires is there.
        for key in ("schemaVersion", "id", "name", "host", "port", "username", "domain", "protocol", "display",
                    "redirection", "keyboard", "security", "advanced", "tags", "notes"):
            self.assertIn(key, encoded)
        self.assertEqual(set(encoded["display"]), {"dynamicResolution", "matchScreenResolution", "retina",
                                                   "fixedWidth", "fixedHeight", "startFullscreen",
                                                   "useAllMonitors", "monitorCount"})

    def test_missing_keys_take_defaults(self):
        c = Connection.from_json({"id": "0f8fad5b-d9cb-469f-a165-70867728950e", "name": "Old",
                                  "host": "pc01", "security": {}})
        self.assertEqual(c.port, 3389)
        self.assertTrue(c.display.dynamic_resolution)
        self.assertIs(c.security.agent_access, AgentAccess.ASK)
        self.assertEqual(c.security.authentication_level, 2)

    def test_wrong_types_and_ids_fail_the_store(self):
        for record in ({"id": "not-a-uuid", "name": "x", "host": "y"},
                       {"id": "0f8fad5b-d9cb-469f-a165-70867728950e", "name": "x", "host": "y", "port": "3389"},
                       {"name": "x", "host": "y"}):
            with self.assertRaises(InvalidStore):
                Connection.from_json(record)

    def test_agent_access_unknown_value_falls_back_to_ask(self):
        security = SecuritySettings.from_json({"agentAccess": "sometimes"})
        self.assertIs(security.agent_access, AgentAccess.ASK)
        old = SecuritySettings.from_json({"ignoreCertificateErrors": False, "trustedCertificateFingerprints":
                                          ["AA:BB"], "disableNLA": True, "authenticationLevel": 0,
                                          "promptForCredentials": False})
        self.assertIs(old.agent_access, AgentAccess.ASK)
        self.assertTrue(old.disable_nla and old.authentication_level == 0 and not old.prompt_for_credentials)
        self.assertTrue(old.trusts("aabb"))

    def test_fingerprint_comparison_ignores_separators_and_case(self):
        security = SecuritySettings(trusted_certificate_fingerprints=["AB:CD:01"])
        self.assertTrue(security.trusts("abcd01"))
        self.assertTrue(security.trusts("ab cd 01"))
        self.assertFalse(security.trusts("abcd02"))
        self.assertFalse(security.trusts(""))

    def test_dates_are_seconds_since_2001(self):
        moment = datetime(2020, 1, 6, 10, 40, tzinfo=timezone.utc)
        seconds = datetime_to_reference(moment)
        self.assertEqual(reference_to_datetime(seconds), moment)
        self.assertEqual(datetime_to_reference(datetime(2001, 1, 1, tzinfo=timezone.utc)), 0)
        c = Connection(name="PC", host="pc01", last_connected=seconds)
        self.assertEqual(c.last_connected_datetime, moment)


class KeyboardSettingsTests(unittest.TestCase):
    def test_text_input_falls_back_to_the_mac_unicode_flag(self):
        self.assertIs(KeyboardSettings.from_json({"unicodeTextInput": True}).text_input, TextInput.UNICODE)
        self.assertIs(KeyboardSettings.from_json({}).text_input, TextInput.SMART)
        self.assertIs(KeyboardSettings.from_json({"unicodeTextInput": True, "textInput": "scancodes"}).text_input,
                      TextInput.SCANCODES)
        self.assertIs(KeyboardSettings.from_json({"textInput": "future"}).text_input, TextInput.SMART)

    def test_unicode_flag_follows_text_input(self):
        for mode, flag in ((TextInput.UNICODE, True), (TextInput.SMART, False), (TextInput.SCANCODES, False)):
            encoded = KeyboardSettings(text_input=mode).to_json()
            self.assertEqual((encoded["textInput"], encoded["unicodeTextInput"]), (mode.value, flag))
        keyboard = KeyboardSettings(text_input=TextInput.SCANCODES)
        keyboard.unicode_text_input = False
        self.assertIs(keyboard.text_input, TextInput.SCANCODES)
        keyboard.unicode_text_input = True
        self.assertIs(keyboard.text_input, TextInput.UNICODE)

    def test_layout_override_notation(self):
        for notation, klid in (("00000407", 0x407), ("0x407", 0x407), ("407", 0x407), ("00010409", 0x10409)):
            self.assertEqual(KeyboardSettings.from_json({"layoutOverride": notation}).layout_override, klid)
        self.assertIsNone(KeyboardSettings.from_json({"layoutOverride": "zz"}).layout_override)
        self.assertEqual(KeyboardSettings(layout_override=0x807).to_json()["layoutOverride"], "00000807")


class ConnectionHelpersTests(unittest.TestCase):
    def test_validate(self):
        Connection(name="PC", host="pc01").validate()
        for change in (lambda c: setattr(c, "host", "  "), lambda c: setattr(c, "port", 0),
                       lambda c: setattr(c.display, "fixed_width", 0),
                       lambda c: setattr(c.display, "monitor_count", 0),
                       lambda c: setattr(c.display, "desktop_scale_factor", 99)):
            c = Connection(name="PC", host="pc01")
            change(c)
            with self.assertRaises(InvalidConnection):
                c.validate()
        with self.assertRaises(UnsupportedSchema):
            Connection(name="PC", host="pc01", schema_version=2).validate()

    def test_vnc_url(self):
        self.assertIsNone(Connection(name="PC", host="pc01").vnc_url)
        self.assertEqual(Connection(name="M", host="m4-mini", port=5900, protocol=ConnectionProtocol.VNC).vnc_url,
                         "vnc://m4-mini:5900")
        self.assertEqual(Connection(name="V6", host="2001:db8::1", port=5900,
                                    protocol=ConnectionProtocol.VNC).vnc_url, "vnc://[2001:db8::1]:5900")

    def test_shared_drives_only_enabled_writable_with_master_switch(self):
        c = Connection(name="PC", host="pc01")
        c.redirection.drives = [DriveMapping("A", "/tmp/a"), DriveMapping("B", "/tmp/b", enabled=False),
                                DriveMapping("C", "/tmp/c", read_only=True), DriveMapping("Home", "~/x")]
        self.assertEqual(c.redirection.shared_drives(), [])
        c.redirection.drive_redirection = True
        shared = c.redirection.shared_drives()
        self.assertEqual([name for name, _path in shared], ["A", "Home"])
        self.assertFalse(shared[1][1].startswith("~"))


if __name__ == "__main__":
    unittest.main()
