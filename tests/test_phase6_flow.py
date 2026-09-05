"""Phase 6 - Flow control and cancellation: bounded buffers with declared
overflow policy, cancel-before-dispatch, cancel-mid-stream drains."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dmh.capabilities import HOST_CAPABILITIES
from dmh.child_runtime import ChildRuntime
from dmh.flow import BoundedEventBuffer
from dmh.host import Host, MAX_STEPS_PER_TURN
from dmh.native import ScriptedNative, NATIVE_CAPABILITIES
from dmh.provider import HarnessProvider, StreamItem


class Phase6FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmh-p6-")

    def test_bounded_buffer_drop_oldest(self):
        buf = BoundedEventBuffer(capacity=3, policy="drop_oldest")
        for i in (1, 2, 3):
            self.assertEqual(buf.push(i), ("ok", 0))
        status, dropped = buf.push(4)
        self.assertEqual(status, "dropped")
        self.assertEqual(dropped, 1)
        self.assertEqual(list(buf.drain()), [2, 3, 4])

    def test_bounded_buffer_pause_and_resume(self):
        buf = BoundedEventBuffer(capacity=2, policy="pause")
        self.assertEqual(buf.push(1), ("ok", 0))
        self.assertEqual(buf.push(2), ("ok", 0))
        self.assertEqual(buf.push(3), ("paused", 0))
        self.assertEqual(buf.push(4), ("paused", 0))
        self.assertEqual(buf.resume(), "resumed")
        # resume() must release pause state without losing what was buffered;
        # the consumer drains via drain(), then subsequent pushes can land.
        self.assertEqual(list(buf.drain()), [1, 2])
        self.assertEqual(buf.push(5), ("ok", 0))

    def test_bounded_buffer_pause_resume_preserves_queue_does_not_drop(self):
        """Regression: pause is documented as lossless. resume() must NOT
        silently empty the queue; consumers own the drain."""
        buf = BoundedEventBuffer(capacity=2, policy="pause")
        buf.push("a")
        buf.push("b")
        self.assertEqual(buf.push("c"), ("paused", 0))
        self.assertEqual(buf.push("d"), ("paused", 0))
        self.assertEqual(buf.resume(), "resumed")
        # Items a and b are still queued; resume() never dropped them.
        self.assertEqual(len(buf), 2)
        self.assertEqual(list(buf.drain()), ["a", "b"])

    def test_cancel_before_dispatch_appends_abort_event(self):
        host = Host(store_dir=self.tmp)
        host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
        s = host.create_session("default")
        s.bind("native")
        s.cancel()  # cancel before any step is dispatched
        s.submit("do not run")
        kinds = [e.kind for e in s.log.events]
        self.assertIn("turn/abort-before-dispatch", kinds)
        self.assertNotIn("assistant/message", kinds)
        turn_end = [e for e in s.log.events if e.kind == "turn/end"][0]
        self.assertEqual(turn_end.payload["status"], "aborted")
        host.stop_all()

    def test_cancel_midstream_drains_to_quiescence(self):
        rt = ChildRuntime(
            script=[{"slow_say": "this is a long stream of text being emitted", "delay": 0.2}],
        )
        rt.launch()
        rt.initialize(HOST_CAPABILITIES, {})
        rt.configure({})
        rt.open_turn(1, [{"role": "user", "content": "x"}])
        gen = rt.run_step()
        first = next(gen)
        self.assertEqual(first.kind, "chunk")
        rt.cancel()  # child observes the fused abort between chunks
        items = list(gen)
        final = items[-1]
        self.assertEqual(final.kind, "step_end")
        self.assertEqual(final.payload["aborted"], "drained")
        rt.stop()

    def test_max_steps_exceeded_yields_turn_abort_not_turn_end_ok(self):
        """Regression: when MAX_STEPS_PER_TURN is reached without a clean turn_end,
        the host must log turn/abort{reason:max_steps_exceeded} instead of
        silently appending turn/end{status:ok}."""

        class LimitLooper(HarnessProvider):
            runtime_id = "limitloop"

            def capabilities(self):
                return dict(NATIVE_CAPABILITIES)

            def initialize(self, host_caps, constraints):
                return {
                    "abiVersion": 2,
                    "runtimeInfo": {"name": "limitloop"},
                    "runtimeCapabilities": dict(NATIVE_CAPABILITIES),
                    "schemaHash": self.schema_hash(),
                }

            def configure(self, snapshot):
                pass

            def open_turn(self, epoch, projection):
                pass

            def run_step(self, resume=None):
                yield StreamItem("chunk", {"text": "still working"})
                yield StreamItem("step_end", {"turn_end": False})

            def cancel(self):
                pass

            def stop(self):
                pass

        host = Host(store_dir=self.tmp)
        host.register("looper", lambda: LimitLooper())
        s = host.create_session("default")
        s.bind("looper")
        s.submit("never finishes")

        # MAX_STEPS_PER_TURN step_start events prove the loop ran to its cap.
        step_starts = [e for e in s.log.events if e.kind == "step/start"]
        self.assertEqual(len(step_starts), MAX_STEPS_PER_TURN)
        # The host must surface turn/abort, never turn/end{status:ok}.
        ok_ends = [
            e for e in s.log.events
            if e.kind == "turn/end" and e.payload.get("status") == "ok"
        ]
        self.assertEqual(ok_ends, [])
        aborts = [e for e in s.log.events if e.kind == "turn/abort"]
        reasons = [a.payload.get("reason") for a in aborts]
        self.assertIn("max_steps_exceeded", reasons,
                      f"expected max_steps_exceeded, got: {reasons}")
        host.stop_all()

    def test_slow_consumer_overflow_is_logged_not_lost(self):
        host = Host(store_dir=self.tmp)
        host.register(
            "native",
            lambda: ScriptedNative(
                sandbox=host.sandbox,
                script=[{"say": "a " * 40}, {"end": True}],
            ),
        )
        s = host.create_session("default")
        consumed = []
        s.register_sink(consumed.append, capacity=2)
        s.bind("native")
        s.submit("flood")
        overflow = [e for e in s.log.events if e.kind == "overflow/dropped"]
        self.assertTrue(len(overflow) >= 1)
        self.assertGreater(overflow[0].payload["count"], 0)
        host.stop_all()


if __name__ == "__main__":
    unittest.main()
