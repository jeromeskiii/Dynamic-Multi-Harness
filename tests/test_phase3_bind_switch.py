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
from dmh.native import ScriptedNative, NATIVE_CAPABILITIES
from dmh.provider import HarnessProvider, StreamItem


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

    def test_switch_runtime_log_records_unbind_and_switch_before_old_stop(self):
        """Contract: log claims the new binding before transport tear-down.
        When the outgoing provider's stop() runs, the session log already
        contains runtime/unbind, runtime/switch, and the incoming bind events."""

        class SlowStopProvider(HarnessProvider):
            runtime_id = "slowstop"

            def __init__(self, runtime_id, log_ref):
                self._rid = runtime_id
                self._log_ref = log_ref
                self.stop_snapshot = None

            def capabilities(self):
                return dict(NATIVE_CAPABILITIES)

            def initialize(self, host_caps, constraints):
                return {
                    "abiVersion": 2,
                    "runtimeInfo": {"name": self._rid, "version": "0.0", "vendor": "test"},
                    "runtimeCapabilities": dict(NATIVE_CAPABILITIES),
                    "schemaHash": self.schema_hash(),
                }

            def configure(self, snapshot):
                pass

            def open_turn(self, epoch, projection):
                pass

            def run_step(self, resume=None):
                yield StreamItem("chunk", {"text": "ok"})
                yield StreamItem("step_end", {"turn_end": True})

            def cancel(self):
                pass

            def stop(self):
                # Snapshot what the log looks like at the moment old.stop runs.
                self.stop_snapshot = [e.kind for e in self._log_ref.events]

        host = build_host(self.tmp)
        host.register(
            "slow_a",
            lambda: SlowStopProvider("slow_a", host.sessions.get(next(iter(host.sessions)), None) or None),
        )

        # Build a host whose session we have a handle to. We'll bind slow_a
        # to a session created first, then register slow_b with a snapshot
        # back-pointer updated after bind.

        # Simpler: hold the session and rewrite the snapshot via a class
        # attribute updater.

        builder = {"log_ref": None}

        class SlowStopWithLog(HarnessProvider):
            runtime_id = "slowlog"

            def __init__(self):
                self.stop_snapshot = None

            def capabilities(self):
                return dict(NATIVE_CAPABILITIES)

            def initialize(self, host_caps, constraints):
                return {
                    "abiVersion": 2,
                    "runtimeInfo": {"name": self.__class__.__name__},
                    "runtimeCapabilities": dict(NATIVE_CAPABILITIES),
                    "schemaHash": self.schema_hash(),
                }

            def configure(self, snapshot):
                pass

            def open_turn(self, epoch, projection):
                pass

            def run_step(self, resume=None):
                yield StreamItem("chunk", {"text": "ok"})
                yield StreamItem("step_end", {"turn_end": True})

            def cancel(self):
                pass

            def stop(self):
                self.stop_snapshot = [e.kind for e in builder["log_ref"].events]

        host.register("ss_a", lambda: SlowStopWithLog())
        host.register("ss_b", lambda: SlowStopWithLog())

        s = host.create_session("default")
        builder["log_ref"] = s.log
        s.bind("ss_a")
        s.submit("first")
        ss_a = s.provider

        s.switch_runtime("ss_b", reason="specialist")
        ss_b = s.provider

        # Old provider's stop() was called during switch_runtime; we asked
        # it to record what the log looked like at that moment.
        snapshot_at_old_stop = ss_a.stop_snapshot
        self.assertIsNotNone(
            snapshot_at_old_stop,
            "old provider's stop() must have run during switch_runtime",
        )
        # At the moment old.stop runs, the log must already show the new
        # unbind, new switch, and the new runtime/bind events.
        self.assertIn("runtime/unbind", snapshot_at_old_stop)
        self.assertIn("runtime/switch", snapshot_at_old_stop)
        bind_indices = [
            i for i, k in enumerate(snapshot_at_old_stop) if k == "runtime/bind"
        ]
        self.assertEqual(
            len(bind_indices), 2,
            "expected two runtime/bind events (initial + new) at old.stop time",
        )
        switch_index = snapshot_at_old_stop.index("runtime/switch")
        # Order: switch precedes the *new* (last) bind in the snapshot.
        self.assertLess(switch_index, bind_indices[-1])
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
