"""Phase 3 - Bind policy and runtime/switch: fail-closed presets, turn-boundary
rebinding, epoch events, namespaced step ids."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dmh.child_runtime import ChildRuntime
from dmh.errors import HarnessError
from dmh.host import Host
from dmh.native import ScriptedNative


def build_host(store, policy=None):
    host = Host(store_dir=store, approval_policy=policy)
    host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
    host.register("child", lambda: ChildRuntime(sandbox=host.sandbox))
    return host


class Phase3BindSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmh-p3-")

    def tearDown(self):
        pass

    def test_bind_records_launch_initialize_bind(self):
        host = build_host(self.tmp)
        s = host.create_session("default")
        s.bind("child")
        kinds = [e.kind for e in s.log.events]
        self.assertIn("runtime/launch", kinds)
        self.assertIn("runtime/initialize", kinds)
        bind = [e for e in s.log.events if e.kind == "runtime/bind"][0]
        self.assertEqual(bind.payload["runtime"], "child")
        self.assertTrue(bind.payload["caps"]["streaming"])
        self.assertTrue(bind.payload["schema_hash"].startswith("sha256:"))
        host.stop_all()

    def test_coding_preset_fails_closed_on_child(self):
        host = build_host(self.tmp)
        s = host.create_session("coding")
        with self.assertRaises(HarnessError) as ctx:
            s.bind("child")  # child lacks sandbox.world
        self.assertEqual(ctx.exception.code, "CAPABILITY_REQUIRED")
        failed = [e for e in s.log.events if e.kind == "runtime/bind-failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].payload["errorCode"], "CAPABILITY_REQUIRED")
        self.assertIn("sandbox.world", failed[0].payload["data"]["missing"])
        # the native driver shares the host sandbox world in-process
        s2 = host.create_session("coding")
        s2.bind("native")
        self.assertTrue(s2.effective_caps["sandbox.world"])
        host.stop_all()

    def test_switch_mid_turn_rejected(self):
        host = build_host(self.tmp)
        s = host.create_session("default")
        s.bind("native")
        s.provider.cancel()  # make the turn abort immediately? No - keep it simple:
        # open a turn by submitting; but run_turn completes synchronously, so
        # simulate an open turn state instead:
        s.turn_open = True
        with self.assertRaises(HarnessError) as ctx:
            s.switch_runtime("child", "cost")
        self.assertEqual(ctx.exception.code, "TURN_OPEN")
        host.stop_all()

    def test_switch_at_turn_boundary_rebinds_with_epoch(self):
        host = build_host(self.tmp)
        s = host.create_session("default")
        s.bind("native")
        s.submit("hello")
        self.assertEqual(s.epoch, 1)
        s.switch_runtime("child", reason="specialist")
        self.assertEqual(s.epoch, 2)
        kinds = [e.kind for e in s.log.events]
        self.assertIn("runtime/switch", kinds)
        switch = [e for e in s.log.events if e.kind == "runtime/switch"][0]
        self.assertEqual(
            (switch.payload["from"], switch.payload["to"], switch.payload["epoch"]),
            ("native", "child", 2),
        )
        s.submit("continue on child")
        text = s.log.last_assistant_text()
        self.assertIn("child-echo: continue on child", text)
        # two runtimes, one session: step ids are namespaced, never collide
        step_ids = [e.payload["step_id"] for e in s.log.events if e.kind == "step/start"]
        self.assertEqual(len(step_ids), len(set(step_ids)))
        self.assertTrue(step_ids[0].startswith("native:"))
        self.assertTrue(step_ids[-1].startswith("child:"))
        host.stop_all()

    def test_switch_requires_replay_from_log(self):
        host = build_host(self.tmp)
        host.register(
            "child-noreplay",
            lambda: ChildRuntime(
                sandbox=host.sandbox, extra_args=["--omit-cap", "replay.from_log"]
            ),
        )
        s = host.create_session("default")
        s.bind("native")
        s.submit("hello")
        with self.assertRaises(HarnessError) as ctx:
            s.switch_runtime("child-noreplay", reason="cheaper")
        self.assertEqual(ctx.exception.code, "CAPABILITY_REQUIRED")
        # old binding survives the refused switch
        self.assertEqual(s.runtime_id, "native")
        s.submit("still native")
        self.assertIn("native-echo: still native", s.log.last_assistant_text())
        host.stop_all()


if __name__ == "__main__":
    unittest.main()
