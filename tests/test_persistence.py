"""Tests for SessionStore, Manifest, and Integrity verification."""

import json
import os
import tempfile
import unittest

from dmh.host import Host
from dmh.native import ScriptedNative
from dmh.persistence import SessionMetadata, SessionStore


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmh-persist-")
        self.store = SessionStore(self.tmp)

    def test_record_and_get_metadata(self):
        meta = SessionMetadata(
            session_id="test-sess-01",
            preset="default",
            current_runtime="native",
            epoch=2,
            turn_count=3,
            event_count=15,
            created_at=1000.0,
            updated_at=1050.0,
        )
        self.store.record_session(meta)
        retrieved = self.store.get_metadata("test-sess-01")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.session_id, "test-sess-01")
        self.assertEqual(retrieved.current_runtime, "native")
        self.assertEqual(retrieved.turn_count, 3)
        self.assertEqual(retrieved.epoch, 2)

    def test_host_manifest_sync(self):
        host = Host(store_dir=self.tmp)
        host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
        session = host.create_session("default")
        session.bind("native")
        session.submit("Hello persistence")

        meta = host.store.get_metadata(session.id)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.current_runtime, "native")
        self.assertEqual(meta.turn_count, 1)
        self.assertGreater(meta.event_count, 0)

        # Resume in a fresh host
        host2 = Host(store_dir=self.tmp)
        host2.register("native", lambda: ScriptedNative(sandbox=host2.sandbox))
        resumed = host2.resume_session(session.id)
        self.assertEqual(resumed.id, session.id)
        self.assertEqual(resumed.turn_no, 1)
        self.assertEqual(resumed.runtime_id, "native")
        self.assertGreaterEqual(len(resumed.log.events), meta.event_count)
        host.stop_all()
        host2.stop_all()

    def test_integrity_verification_success(self):
        host = Host(store_dir=self.tmp)
        host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
        session = host.create_session("default")
        session.bind("native")
        session.submit("Turn 1")
        session.submit("Turn 2")

        ok, msg = host.store.verify_integrity(session.id)
        self.assertTrue(ok, msg)
        self.assertIn("events verified", msg)
        host.stop_all()

    def test_integrity_verification_detects_corruption(self):
        host = Host(store_dir=self.tmp)
        host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
        session = host.create_session("default")
        session.bind("native")
        session.submit("Turn 1")

        path = host.store.get_session_path(session.id)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("NOT_VALID_JSON\n")

        ok, msg = host.store.verify_integrity(session.id)
        self.assertFalse(ok)
        self.assertIn("corrupted JSON", msg)
        host.stop_all()

    def test_delete_session(self):
        host = Host(store_dir=self.tmp)
        host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
        session = host.create_session("default")
        session.bind("native")

        self.assertTrue(os.path.isfile(host.store.get_session_path(session.id)))
        self.assertTrue(host.store.delete_session(session.id))
        self.assertFalse(os.path.isfile(host.store.get_session_path(session.id)))
        self.assertIsNone(host.store.get_metadata(session.id))
        host.stop_all()


if __name__ == "__main__":
    unittest.main()
