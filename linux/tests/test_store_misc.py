"""App settings, Wake-on-LAN, the editor draft and the credential stores."""

import json
import socket
import unittest

from weitblick.i18n import _
from weitblick.store import (KEEP_PASSWORD, REMOVE_PASSWORD, AppSettings, Connection, ConnectionDraft,
                             InMemoryCredentialStore, PasswordChange)
from weitblick.store import wake_on_lan
from weitblick.store.credentials import CredentialStoreError, SecretServiceCredentialStore, _safe
from weitblick.store.settings import agent_control_enabled_in_file
from weitblick.store.wake_on_lan import WakeOnLANError

from tests.test_store_jump import TemporaryFolder


class AppSettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryFolder()
        self.addCleanup(self.folder.cleanup)
        self.path = self.folder.path / "settings.json"

    def test_defaults_without_a_file(self):
        settings = AppSettings(self.path)
        self.assertFalse(settings.sessions_in_own_windows)
        self.assertTrue(settings.confirm_close_session)
        self.assertEqual(settings.overview_sort, "name")
        self.assertFalse(settings.agent_control_enabled)
        self.assertEqual(settings.system_shortcut_capture, "fullscreen")
        self.assertFalse(self.path.exists())

    def test_set_saves_camel_case_keys_and_notifies(self):
        settings = AppSettings(self.path)
        heard = []
        settings.add_listener(heard.append)
        settings.agent_control_enabled = True
        settings.overview_sort = "recent"
        settings.overview_sort = "recent"  # unchanged: no save, no notification
        self.assertEqual(heard, ["agentControlEnabled", "overviewSort"])
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored, {"agentControlEnabled": True, "overviewSort": "recent"})
        self.assertTrue(agent_control_enabled_in_file(self.path))
        self.assertTrue(AppSettings(self.path).agent_control_enabled)

    def test_invalid_values_rejected_and_ignored_in_the_file(self):
        settings = AppSettings(self.path)
        with self.assertRaises(ValueError):
            settings.system_shortcut_capture = "sometimes"
        with self.assertRaises(ValueError):
            settings.confirm_close_session = "yes"
        self.path.write_text(json.dumps({"systemShortcutCapture": "sometimes", "confirmCloseSession": 1,
                                         "otherVersionKey": [1]}))
        settings = AppSettings(self.path)
        self.assertEqual(settings.system_shortcut_capture, "fullscreen")
        self.assertTrue(settings.confirm_close_session)
        settings.sessions_in_own_windows = True
        self.assertEqual(json.loads(self.path.read_text())["otherVersionKey"], [1])

    def test_reload_notifies_changed_keys(self):
        settings = AppSettings(self.path)
        heard = []
        settings.add_listener(heard.append)
        self.path.write_text(json.dumps({"agentControlEnabled": True}))
        settings.reload()
        self.assertEqual(heard, ["agentControlEnabled"])
        self.assertFalse(agent_control_enabled_in_file(self.folder.path / "missing.json"))
        self.path.write_text("{ broken")
        self.assertFalse(agent_control_enabled_in_file(self.path))


class WakeOnLANTests(unittest.TestCase):
    def test_parses_common_mac_notations(self):
        expected = bytes([0x00, 0x1C, 0x42, 0xAB, 0xCD, 0xEF])
        for text in ("00:1c:42:ab:cd:ef", "00-1C-42-AB-CD-EF", "001c42abcdef", " 00:1C:42:AB:CD:EF "):
            self.assertEqual(wake_on_lan.mac_address(text), expected, text)
        for text in ("", "00:1c:42:ab:cd", "00:1c:42:ab:cd:ef:01", "00:1c:42:ab:cd:eg"):
            self.assertIsNone(wake_on_lan.mac_address(text), text)

    def test_magic_packet_is_sync_stream_and_sixteen_copies(self):
        mac = bytes([1, 2, 3, 4, 5, 6])
        packet = wake_on_lan.magic_packet(mac)
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        for copy in range(16):
            self.assertEqual(packet[6 + copy * 6:12 + copy * 6], mac)

    def test_invalid_input_raises_before_sending(self):
        with self.assertRaises(WakeOnLANError) as raised:
            wake_on_lan.wake(["nope"], ["127.0.0.1"])
        self.assertEqual(raised.exception, WakeOnLANError("invalidMACAddress", "nope"))
        with self.assertRaises(WakeOnLANError) as raised:
            wake_on_lan.wake(["02:00:00:00:00:01"], ["10.0.0"])
        self.assertEqual(raised.exception, WakeOnLANError("invalidBroadcastAddress", "10.0.0"))

    def test_sends_the_packet_to_the_given_address_and_port(self):
        """Over loopback, so no packet leaves the machine."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(("127.0.0.1", 0))
            receiver.settimeout(2)
            wake_on_lan.wake(["02:00:00:00:00:01"], ["127.0.0.1"], port=receiver.getsockname()[1])
            data = receiver.recv(200)
        self.assertEqual(data, wake_on_lan.magic_packet(bytes([2, 0, 0, 0, 0, 1])))


class ConnectionDraftTests(unittest.TestCase):
    def test_problems(self):
        draft = ConnectionDraft.new()
        self.assertEqual(draft.problem, _("Enter a host."))
        draft.connection.host = "pc 01"
        self.assertEqual(draft.problem, _("The host can’t contain spaces."))
        draft.connection.host = "  pc01.example "
        self.assertIsNone(draft.problem)
        draft.connection.port = 0
        self.assertIsNotNone(draft.problem)
        draft.connection.port = 3389
        draft.connection.display.match_screen_resolution = False
        draft.connection.display.fixed_width = 100
        self.assertEqual(draft.problem, _("The fixed resolution must be between 200 and 8192 pixels."))

    def test_saved_trims_and_names_after_the_host(self):
        draft = ConnectionDraft.new()
        draft.connection.host = " pc01.example "
        draft.connection.username = " anna "
        draft.connection.advanced.alternate_shell = "  "
        draft.connection.advanced.wake_on_lan_mac_addresses = ["02:00:00:00:00:01", " ", ""]
        saved = draft.saved()
        self.assertEqual((saved.host, saved.name, saved.username), ("pc01.example", "pc01.example", "anna"))
        self.assertIsNone(saved.advanced.alternate_shell)
        self.assertEqual(saved.advanced.wake_on_lan_mac_addresses, ["02:00:00:00:00:01"])
        saved.validate()
        self.assertEqual(draft.connection.host, " pc01.example ")  # the draft itself stays as typed

    def test_password_change(self):
        draft = ConnectionDraft.editing(Connection(name="PC", host="pc01"), has_stored_password=True)
        self.assertEqual(draft.password_change, KEEP_PASSWORD)
        draft.remove_stored_password = True
        self.assertEqual(draft.password_change, REMOVE_PASSWORD)
        draft.password = "neu"
        self.assertEqual(draft.password_change, PasswordChange("set", "neu"), "a typed password wins over removing")


class CredentialStoreTests(unittest.TestCase):
    def test_in_memory_store(self):
        store = InMemoryCredentialStore()
        connection_id = "0F8FAD5B-D9CB-469F-A165-70867728950E"
        self.assertIsNone(store.get(connection_id))
        store.set("secret", connection_id, "Weitblick Remote: PC")
        self.assertEqual(store.get(connection_id.lower()), "secret")
        store.delete(connection_id)
        store.delete(connection_id)  # deleting nothing is fine
        self.assertIsNone(store.get(connection_id))

    def test_secret_service_errors_carry_no_secrets(self):
        class FakeGLibError(Exception):
            domain = "g-dbus-error-quark"
            code = 2

        self.assertEqual(_safe(FakeGLibError("password=hunter2")), "g-dbus-error-quark:2")
        self.assertEqual(_safe(ValueError("hunter2")), "ValueError")
        self.assertTrue(issubclass(CredentialStoreError, Exception))
        # Constructing the store touches nothing; only calls reach the keyring.
        SecretServiceCredentialStore()


if __name__ == "__main__":
    unittest.main()
