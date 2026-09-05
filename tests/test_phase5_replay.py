"""Phase 5 - Replay fixture: same log, two runtimes, identical surface
projection up to the switch marker; reconstruction comes from the log, never
from a runtime's leftover RAM."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dmh.child_runtime import ChildRuntime
from dmh.host import Host
from dmh.native import ScriptedNative


def build_host(store):
    host = Host(store_dir=store)
    host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
    host.register("child", lambda: ChildRuntime(sandbox=host.sandbox))
    return host


class Phase5ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmh-p5-")

    def tearDown(self):
        pass

    def test_same_log_two_runtimes_identical_projection_up_to_switch(self):
        host = build_host(self.tmp)
        # session A: turn 1 on native, switch, turn 2 on child
        s = host.create_session("default")
        s.bind("native")
        s.submit("hello")
        pre = [(e.kind, e.payload) for e in s.log.events_until_switch()]
        s.switch_runtime("child", reason="specialist")
        s.submit("continue")

        # session B: adopt the pre-switch surface, bind child, same input
        s2 = host.create_session("default")
        for kind, payload in pre:
            s2.log.append(kind, payload)
        s2.bind("child")
        s2.submit("continue")

        # identical model-visible derivation across runtimes and sessions
        self.assertEqual(s.log.derive_messages(), s2.log.derive_messages())
        # identical continuation: the child rebuilt from the same projection
        self.assertEqual(s.log.last_assistant_text(), s2.log.last_assistant_text())
        self.assertIn("child-echo: continue (context=3)", s.log.last_assistant_text())
        # the pre-switch window is exactly what the replay session adopted:
        # the first len(pre) surface events of the replay log are pre itself
        self.assertEqual(
            [(e.kind, e.payload) for e in s2.log.surface_events()[:len(pre)]],
            pre,
        )
        host.stop_all()

    def test_replay_from_disk_projects_identically(self):
        host = build_host(self.tmp)
        s = host.create_session("default")
        s.bind("native")
        s.submit("hello")
        s.submit("again")
        events_before = [(e.kind, e.payload) for e in s.log.events]
        reloaded = s.log.__class__(s.id, host.store_dir)
        reloaded.reload()
        events_after = [(e.kind, e.payload) for e in reloaded.events]
        self.assertEqual(events_before, events_after)
        reloaded.close()
        host.stop_all()


if __name__ == "__main__":
    unittest.main()
