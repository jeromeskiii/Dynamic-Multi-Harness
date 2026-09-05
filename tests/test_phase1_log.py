"""Phase 1 - Host kernel: append-only log, surface vs control events,
derivation, fork safety, switch-marker projection."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dmh.events import SessionLog, SURFACE_KINDS
from dmh.errors import HarnessError


class Phase1LogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmh-p1-")
        self._logs = []

    def _log(self, session_id):
        log = SessionLog(session_id, self.tmp)
        self._logs.append(log)
        return log

    def tearDown(self):
        for log in self._logs:
            log.close()

    def _fork(self, parent, new_id):
        child = parent.fork(new_id)
        self._logs.append(child)
        return child

    def test_append_persists_and_replay_identical(self):
        log = self._log("s1")
        log.append("turn/start", {"turn": 1})
        log.append("user/message", {"text": "hello"})
        log.append("assistant/message", {"text": "hi"})
        log.append("turn/end", {"turn": 1})
        events_before = [(e.kind, e.payload) for e in log.events]
        replay = self._log("s1")
        replay.reload()
        events_after = [(e.kind, e.payload) for e in replay.events]
        self.assertEqual(events_before, events_after)

    def test_unknown_event_kind_rejected_fail_loud(self):
        log = self._log("s2")
        with self.assertRaises(ValueError):
            log.append("assistant/private-ram")

    def test_derive_messages_surface_only(self):
        log = self._log("s3")
        log.append("runtime/bind", {"runtime": "native"})
        log.append("turn/start", {"turn": 1})
        log.append("user/message", {"text": "hi"})
        log.append("assistant/chunk", {"text": "hel"})
        log.append("assistant/chunk", {"text": "lo"})
        log.append("assistant/message", {"text": "hello"})
        log.append("approval/request", {"tool": "fs.read"})  # control, log-only
        log.append("turn/end", {"turn": 1})
        messages = log.derive_messages()
        self.assertEqual(
            messages,
            [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": "hello"}],
        )

    def test_fork_refuses_open_turn(self):
        log = self._log("s4")
        log.append("turn/start", {"turn": 1})
        with self.assertRaises(HarnessError) as ctx:
            log.fork("s4-child")
        self.assertEqual(ctx.exception.code, "TURN_OPEN")

    def test_fork_does_not_mutate_parent(self):
        log = self._log("s5")
        log.append("turn/start", {"turn": 1})
        log.append("user/message", {"text": "x"})
        log.append("turn/end", {"turn": 1})
        child = self._fork(log, "s5-child")
        child.append("assistant/message", {"text": "only in child"})
        self.assertEqual(len(log.events), 3)
        self.assertEqual(len(child.events), 4)

    def test_events_until_switch_cuts_at_marker(self):
        log = self._log("s6")
        log.append("turn/start", {"turn": 1})
        log.append("user/message", {"text": "a"})
        log.append("assistant/message", {"text": "b"})
        log.append("turn/end", {"turn": 1})
        log.append("runtime/switch", {"from": "native", "to": "child"})
        log.append("turn/start", {"turn": 2})
        pre = [e.kind for e in log.events_until_switch()]
        self.assertEqual(
            pre, ["turn/start", "user/message", "assistant/message", "turn/end"]
        )

    def test_surface_kinds_do_not_include_control(self):
        self.assertIn("runtime/bind", SURFACE_KINDS)
        self.assertNotIn("approval/request", SURFACE_KINDS)
        self.assertNotIn("runtime/launch", SURFACE_KINDS)


if __name__ == "__main__":
    unittest.main()
