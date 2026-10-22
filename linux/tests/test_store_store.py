"""The connections file and the library on top (ports of ConnectionStoreTests and ConnectionLibraryTests)."""

import json
import unittest

from weitblick.i18n import _
from weitblick.store import (Connection, ConnectionLibrary, ConnectionStore, DuplicateID,
                             InMemoryCredentialStore, ImportSource, InvalidConnection, InvalidStore,
                             MissingConnection, SORT_RECENT, UnsupportedSchema)
from weitblick.store.jump import JumpImporter
from weitblick.store.store import encode_document

from tests.test_store_jump import TemporaryFolder, synthetic_jump


class ConnectionStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryFolder()
        self.addCleanup(self.folder.cleanup)
        self.path = self.folder.store_path

    def test_round_trip_and_atomic_write(self):
        self.folder.write_jump(synthetic_jump())
        imported = JumpImporter(self.folder.path).plan(existing=[]).entries[0].connection
        store = ConnectionStore(self.path)
        self.assertEqual(store.load(), [])
        store.insert(imported)
        first = self.path.read_bytes()
        changed = imported.copy()
        changed.notes = "Synthetic notes"
        store.update(changed)
        self.assertNotEqual(first, self.path.read_bytes())
        reloaded = ConnectionStore(self.path)
        self.assertEqual(reloaded.load(), [changed])
        self.assertEqual(sorted(p.name for p in self.folder.path.iterdir()), ["connections.json", "test.jump"])
        store.save()
        self.assertEqual(reloaded.load(), [changed])

    def test_file_format_matches_the_mac_encoder(self):
        connection = Connection(name="Lab", host="PC0001.example")
        ConnectionStore(self.path).insert(connection)
        text = self.path.read_text()
        self.assertTrue(text.startswith('{\n  "connections" : [\n'))
        self.assertIn('"agentAccess" : "ask"', text)
        document = json.loads(text)
        self.assertEqual(document["schemaVersion"], 1)
        self.assertEqual(document["connections"][0]["id"], connection.id)

    def test_unopened_store_mutations_preserve_existing_records(self):
        original = Connection(name="Existing", host="PC0001.example")
        ConnectionStore(self.path).insert(original)
        new_store = ConnectionStore(self.path)
        new_store.save()
        self.assertEqual(new_store.connections, [original])
        added = Connection(name="New", host="PC0002.example")
        another = ConnectionStore(self.path)
        another.insert(added)
        self.assertEqual(another.connections, [original, added])
        change = original.copy()
        change.name = "Changed"
        updating = ConnectionStore(self.path)
        updating.update(change)
        self.assertEqual(updating.connections, [change, added])

    def test_failed_persistence_does_not_change_snapshot(self):
        store = ConnectionStore(self.path)
        connection = Connection(name="Test", host="PC0001.example")
        store.insert(connection)
        self.path.unlink()
        self.path.mkdir()
        change = connection.copy()
        change.name = "Must not persist"
        with self.assertRaises(OSError):
            store.update(change)
        self.assertEqual(store.connections, [connection])
        self.assertEqual([p.name for p in self.folder.path.iterdir()], ["connections.json"])  # no temp left

    def test_crud_duplicate_search_and_sort(self):
        store = ConnectionStore(self.path)
        alpha = Connection(name="Alpha", host="PC0001.example", username="testuser")
        alpha.tags = ["Testing"]
        alpha.last_connected = 100.0
        beta = Connection(name="Beta", host="PC0002.example")
        beta.last_connected = 200.0
        beta.import_source = ImportSource(unique_id="synthetic-2", file_name="synthetic.jump")
        store.insert(beta)
        store.insert(alpha)
        self.assertEqual([c.name for c in store.search()], ["Alpha", "Beta"])
        self.assertEqual([c.name for c in store.search(sort=SORT_RECENT)], ["Beta", "Alpha"])
        self.assertEqual([c.id for c in store.search("TESTING")], [alpha.id])
        self.assertEqual([c.id for c in store.search("pc0002")], [beta.id])
        self.assertEqual([c.id for c in ConnectionStore.filter(store.connections, "alpha")], [alpha.id])
        duplicate = store.duplicate(beta.id)
        self.assertEqual(duplicate.name, _("{name} copy").format(name="Beta"))
        self.assertNotEqual(duplicate.id, beta.id)
        self.assertIsNone(duplicate.import_source)
        self.assertIsNone(duplicate.last_connected)
        self.assertEqual(duplicate.host, beta.host)
        store.delete(duplicate.id)
        self.assertEqual(len(store.connections), 2)
        with self.assertRaises(DuplicateID):
            store.insert(alpha)
        with self.assertRaises(MissingConnection):
            store.delete(duplicate.id)

    def test_natural_name_order(self):
        store = ConnectionStore(self.path)
        for name in ("pc10", "PC2", "pc1"):
            store.insert(Connection(name=name, host=name))
        self.assertEqual([c.name for c in store.search()], ["pc1", "PC2", "pc10"])

    def test_modify_changes_the_stored_copy_not_a_stale_one(self):
        store = ConnectionStore(self.path)
        original = Connection(name="Alpha", host="PC0001.example")
        store.insert(original)
        edited = original.copy()
        edited.notes = "Edited meanwhile"
        store.update(edited)
        result = store.modify(original.id, lambda c: setattr(c, "last_connected", 1000.0))
        self.assertEqual((result.notes, result.last_connected), ("Edited meanwhile", 1000.0))
        self.assertEqual(ConnectionStore(self.path).load(), [result])
        with self.assertRaises(MissingConnection):
            ConnectionStore(self.folder.path / "other.json").modify(edited.id, lambda c: None)

    def test_snapshot_is_a_copy(self):
        store = ConnectionStore(self.path)
        store.insert(Connection(name="Alpha", host="pc01"))
        store.connections[0].name = "Changed outside"
        self.assertEqual(store.connections[0].name, "Alpha")

    def test_listeners_hear_loads_and_changes(self):
        store = ConnectionStore(self.path)
        calls = []
        token = store.add_listener(lambda: calls.append(len(store.connections)))
        store.load()
        store.insert(Connection(name="Alpha", host="pc01"))
        self.assertEqual(calls, [0, 1])
        store.remove_listener(token)
        store.insert(Connection(name="Beta", host="pc02"))
        self.assertEqual(calls, [0, 1])

    def test_built_in_legacy_migration_backs_up_original(self):
        connection_id = "0F8FAD5B-D9CB-469F-A165-70867728950E"
        original = json.dumps([{"id": connection_id, "name": "Old", "host": "PC0001.example"}]).encode()
        self.path.write_bytes(original)
        store = ConnectionStore(self.path)
        loaded = store.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, connection_id)
        self.assertEqual((loaded[0].schema_version, loaded[0].port), (1, 3389))
        self.assertEqual(store.last_migration_backup.read_bytes(), original)
        following = ConnectionStore(self.path)
        self.assertEqual(following.load(), loaded)
        self.assertIsNone(following.last_migration_backup)

    def test_custom_migration_hook_and_failure_backup(self):
        original = b'{"schemaVersion":0,"oldRecords":[]}'
        self.path.write_bytes(original)
        migrated = Connection(name="Migrated", host="PC0001.example")
        seen = []

        def migration(data, version):
            seen.append((data, version))
            return encode_document([migrated])

        store = ConnectionStore(self.path, migration=migration)
        self.assertEqual(store.load(), [migrated])
        self.assertEqual(seen, [(original, 0)])
        self.assertEqual(store.last_migration_backup.read_bytes(), original)
        self.path.write_bytes(original)

        def failing(data, version):
            raise InvalidStore()

        failing_store = ConnectionStore(self.path, migration=failing)
        with self.assertRaises(InvalidStore):
            failing_store.load()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(failing_store.last_migration_backup.read_bytes(), original)

    def test_rejects_future_schema_and_invalid_connections(self):
        data = b'{"schemaVersion":99,"connections":[]}'
        self.path.write_bytes(data)
        store = ConnectionStore(self.path)
        with self.assertRaises(UnsupportedSchema) as raised:
            store.load()
        self.assertEqual(raised.exception, UnsupportedSchema(99))
        self.assertEqual(self.path.read_bytes(), data)
        self.assertIsNone(store.last_migration_backup)
        with self.assertRaises(UnsupportedSchema):
            store.insert(Connection(name="Must not overwrite", host="PC0002.example"))
        self.assertEqual(self.path.read_bytes(), data)
        self.path.unlink()
        invalid = Connection(name="Invalid", host="PC0001.example", port=0)
        with self.assertRaises(InvalidConnection):
            store.insert(invalid)
        self.assertEqual(store.connections, [])

    def test_agent_access_round_trips(self):
        from weitblick.store import AgentAccess

        connection = Connection(name="Lab", host="PC0001.example")
        connection.security.agent_access = AgentAccess.NEVER
        ConnectionStore(self.path).insert(connection)
        self.assertIn('"agentAccess" : "never"', self.path.read_text())
        self.assertIs(ConnectionStore(self.path).load()[0].security.agent_access, AgentAccess.NEVER)


class ConnectionLibraryTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryFolder()
        self.addCleanup(self.folder.cleanup)
        self.credentials = InMemoryCredentialStore()
        self.library = ConnectionLibrary(ConnectionStore(self.folder.store_path), self.credentials)

    def test_first_start_until_something_is_saved(self):
        self.library.load()
        self.assertTrue(self.library.is_first_start)
        self.library.add(Connection(name="PC", host="pc01.example"))
        self.assertFalse(self.library.is_first_start)
        self.assertEqual([c.name for c in self.library.connections], ["PC"])

    def test_unreadable_file_is_reported_and_never_overwritten(self):
        garbage = b"{ kaputt"
        self.folder.store_path.write_bytes(garbage)
        self.library.load()
        self.assertIsNotNone(self.library.load_error)
        self.assertFalse(self.library.is_first_start)
        with self.assertRaises(InvalidStore):
            self.library.add(Connection(name="PC", host="pc01.example"))
        self.assertEqual(self.folder.store_path.read_bytes(), garbage)

    def test_delete_also_removes_the_password_and_duplicate_copies_it(self):
        self.library.load()
        original = Connection(name="PC", host="pc01.example")
        self.library.add(original)
        self.library.set_password("secret", original.id)
        self.assertEqual(self.credentials.labels[original.id], "Weitblick Remote: PC")
        copy = self.library.duplicate(original.id)
        self.assertEqual(copy.name, _("{name} copy").format(name="PC"))
        self.assertEqual(self.library.password(copy.id), "secret")
        self.library.delete([original.id])
        self.assertIsNone(self.library.password(original.id))
        self.assertEqual([c.id for c in self.library.connections], [copy.id])

    def test_mark_connected_keeps_concurrent_edits(self):
        self.library.load()
        connection = Connection(name="PC", host="pc01.example")
        self.library.add(connection)
        connection.notes = "edited"
        self.library.update(connection)
        self.library.mark_connected(connection.id, when=1000.0)
        stored = self.library.connection(connection.id)
        self.assertEqual((stored.notes, stored.last_connected), ("edited", 1000.0))
        self.library.mark_connected("0F8FAD5B-D9CB-469F-A165-70867728950E")  # unknown: only logged

    def test_trust_adds_or_replaces_fingerprints(self):
        self.library.load()
        connection = Connection(name="PC", host="pc01.example")
        self.library.add(connection)
        self.library.trust("AA:BB", connection.id, replacing=False)
        self.library.trust("aabb", connection.id, replacing=False)
        self.assertEqual(self.library.connection(connection.id).security.trusted_certificate_fingerprints, ["AA:BB"])
        self.library.trust("CC:DD", connection.id, replacing=True)
        self.assertEqual(self.library.connection(connection.id).security.trusted_certificate_fingerprints, ["CC:DD"])

    def test_find_by_id_or_name(self):
        self.library.load()
        alpha = Connection(name="Alpha", host="pc01")
        self.library.add(alpha)
        self.library.add(Connection(name="Beta", host="pc02"))
        self.assertEqual(self.library.find(alpha.id.lower()).id, alpha.id)
        self.assertEqual(self.library.find("alpha").id, alpha.id)
        self.assertIsNone(self.library.find("gamma"))


if __name__ == "__main__":
    unittest.main()
