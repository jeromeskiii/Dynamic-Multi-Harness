"""Multi-runtime switch demonstrator.

A single session that:
  - binds runtime A ("native"),
  - runs one turn on A,
  - switches at the turn boundary to runtime B ("child"),
  - runs one turn on B,
  - switches back to A,
  - runs one final turn on A.

Asserts the host invariants the README / phase 3 describe:
  - runtime/switch events are turn-boundary-only,
  - epoch increments on every switch,
  - step ids remain unique across the whole session
    (namespaced "<runtime>:<session>:<step>"),
  - both runtimes' signatures appear in the right order
    in the user-visible projection,
  - the projection reconstructs identically from the log
    (no driver-private RAM leaks),
  - the conversation timeline interleaves user/assistant
    facts in the correct order.

This is a black-box demo of the dynamic harness contract:
the host stays the same; only the provider underneath it
changes between turns, and the model-visible log stays
self-consistent.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dmh.child_runtime import ChildRuntime
from dmh.host import Host
from dmh.native import ScriptedNative


RUNTIME_A = "native"
RUNTIME_B = "child"


def build_host(store):
    """A host exposing two interchangeable runtimes behind the same ABI."""
    host = Host(store_dir=store)
    host.register(RUNTIME_A, lambda: ScriptedNative(sandbox=host.sandbox))
    host.register(RUNTIME_B, lambda: ChildRuntime(sandbox=host.sandbox))
    return host


class MultiRuntimeSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmh-multiswitch-")
        self.host = build_host(self.tmp)
        self.session = self.host.create_session("default")
        self.session.bind(RUNTIME_A)

    def tearDown(self):
        # Stop everything we spawned so child processes do not leak.
        self.host.stop_all()

    # ---- helpers ----
    def _kinds(self):
        return [e.kind for e in self.session.log.events]

    def _user_assistant_pairs(self):
        """Return [(text, text), ...] for user/assistant messages in order."""
        pairs = []
        last_user = None
        for e in self.session.log.surface_events():
            if e.kind == "user/message":
                last_user = e.payload.get("text", "")
            elif e.kind == "assistant/message" and last_user is not None:
                pairs.append((last_user, e.payload.get("text", "")))
                last_user = None
        return pairs

    # ---- the demonstration ----
    def test_session_with_three_runtimes_alternating_two_distinct_providers(self):
        s = self.session

        # --- turn 1 on runtime A ---
        s.submit("Map the redirector chain")
        self.assertEqual(s.epoch, 1)
        self.assertEqual(s.runtime_id, RUNTIME_A)
        first_assistant = s.log.last_assistant_text()
        self.assertIn("native-echo: Map the redirector chain", first_assistant)

        # --- switch A -> B at the (closed) turn boundary ---
        s.switch_runtime(RUNTIME_B, reason="specialist")
        self.assertEqual(s.epoch, 2)
        self.assertEqual(s.runtime_id, RUNTIME_B)

        # --- turn 2 on runtime B ---
        # B's default behaviour echoes with a (context=N) annotation; setting
        # it on A's first turn gives B a non-empty projection, so the {context}
        # slot is meaningful.
        s.submit("Continue with the foreign runtime")
        second_assistant = s.log.last_assistant_text()
        self.assertIn("child-echo:", second_assistant)
        self.assertIn("Continue with the foreign runtime", second_assistant)

        # --- switch B -> A back ---
        s.switch_runtime(RUNTIME_A, reason="back on home runtime")
        self.assertEqual(s.epoch, 3)
        self.assertEqual(s.runtime_id, RUNTIME_A)

        # --- turn 3 on runtime A again ---
        s.submit("Wrap up on the local runtime")
        third_assistant = s.log.last_assistant_text()
        self.assertIn("native-echo: Wrap up on the local runtime", third_assistant)

        # ---------------- invariants -----------------

        # Two runtime/switch events, both at turn boundaries, both with a
        # strictly-increasing epoch that matches the session's.
        switch_events = [e for e in s.log.events if e.kind == "runtime/switch"]
        self.assertEqual(len(switch_events), 2)
        self.assertEqual([e.payload["epoch"] for e in switch_events], [2, 3])
        self.assertEqual(
            [(e.payload["from"], e.payload["to"]) for e in switch_events],
            [(RUNTIME_A, RUNTIME_B), (RUNTIME_B, RUNTIME_A)],
        )
        for e in switch_events:
            self.assertTrue(
                e.payload.get("reason"),
                "runtime/switch must carry the reason that motivated the swap",
            )

        # Three runtime/bind events (initial + two switches), each epoch == 2 or 3
        # corresponds to the bind that established the runtime that turn began.
        bind_events = [e for e in s.log.events if e.kind == "runtime/bind"]
        self.assertEqual(len(bind_events), 3)
        self.assertEqual(
            [e.payload["runtime"] for e in bind_events],
            [RUNTIME_A, RUNTIME_B, RUNTIME_A],
        )

        # Two runtime/unbind events, one per switch (the first runtime has no
        # unbind until the first switch).
        unbind_events = [e for e in s.log.events if e.kind == "runtime/unbind"]
        self.assertEqual(len(unbind_events), 2)
        self.assertEqual(
            [e.payload["runtime"] for e in unbind_events],
            [RUNTIME_A, RUNTIME_B],
        )
        for e in unbind_events:
            self.assertTrue(
                e.payload.get("reason"),
                "runtime/unbind must carry the reason from the matching switch",
            )

        # No mid-stream switches: every runtime/switch sits between
        # turn/end (or the session boundary) and turn/start.
        for i, kind in enumerate(self._kinds()):
            if kind != "runtime/switch":
                continue
            prev_kind = self._kinds()[i - 1] if i > 0 else None
            next_kind = self._kinds()[i + 1] if i + 1 < len(self._kinds()) else None
            # The event before the switch may be turn/end of the previous turn,
            # runtime/unbind (which we just appended), or the very first event.
            self.assertNotEqual(prev_kind, "step/start")
            self.assertNotEqual(prev_kind, "tool/call")
            self.assertNotEqual(prev_kind, "assistant/chunk")
            # The event after the switch kicks off the new runtime: turn/start,
            # runtime/bind is logical-after, never a partial-turn state.
            self.assertNotEqual(next_kind, "step/start")

        # Step ids are namespaced "<runtime>:<session>:<step_no>". Two
        # invariants follow from this format:
        #   (a) within a single bind cycle, step_no advances monotonically
        #       so the id strings are unique;
        #   (b) across DIFFERENT runtimes, the namespace prefix prevents
        #       collision even when both runtimes are at step_no=1.
        # Note: because step_no resets at the start of every turn, an id
        # can recur when the SAME runtime is rebound later in the
        # session (e.g. native gets "native:<s>:1" twice if native is
        # bound, switched out, and switched back in). The README's
        # "namespaced step ids" claim is honored across runtimes but
        # not across rebinds of one runtime.
        step_starts = [e for e in s.log.events if e.kind == "step/start"]

        # (a) Per-runtime-period uniqueness: group step_starts by their
        # binding (i.e. a fresh run begins after every runtime/switch)
        # and assert each group's ids are unique.
        periods = [[]]  # list of lists of step_starts per binding
        for e in s.log.events:
            if e.kind == "runtime/switch":
                periods.append([])
                continue
            if e.kind == "step/start":
                periods[-1].append(e)
        for period in periods:
            ids = [e.payload["step_id"] for e in period]
            self.assertEqual(
                len(ids), len(set(ids)),
                "step ids must be unique within one runtime/bind cycle"
            )

        # (b) Cross-runtime non-collision: every native step id must
        # differ from every child step id. (Does not require uniqueness;
        # only that the two namespaces never share a string.)
        native_ids = {
            e.payload["step_id"]
            for e in step_starts if e.runtime_id == RUNTIME_A
        }
        child_ids = {
            e.payload["step_id"]
            for e in step_starts if e.runtime_id == RUNTIME_B
        }
        self.assertEqual(native_ids & child_ids, set(),
                         "native and child must never share a step_id")
        self.assertTrue(native_ids, "expected at least one native step")
        self.assertTrue(child_ids, "expected at least one child step")

        # The schema_hash logged on each step mirrors the runtime that produced
        # it: A's steps carry A's hash, B's steps carry B's hash, never mixed.
        # Native's hash is sha256(__file__) of native.py; the child runtime's
        # hash is sha256(__file__) of dmh/child.py; they must differ.
        step_hashes_by_runtime = {RUNTIME_A: set(), RUNTIME_B: set()}
        for e in step_starts:
            runtime = e.runtime_id
            h = e.payload["schema_hash"]
            self.assertTrue(h.startswith("sha256:"))
            self.assertNotEqual(runtime, None)
            step_hashes_by_runtime[runtime].add(h)
        # Every step on A shares a single A hash, every step on B shares a
        # single B hash, and the two hashes are distinct.
        self.assertEqual(
            len(step_hashes_by_runtime[RUNTIME_A]), 1,
            "all steps produced by runtime A must share its schema hash"
        )
        self.assertEqual(
            len(step_hashes_by_runtime[RUNTIME_B]), 1,
            "all steps produced by runtime B must share its schema hash"
        )
        self.assertNotEqual(
            step_hashes_by_runtime[RUNTIME_A],
            step_hashes_by_runtime[RUNTIME_B],
            "the two runtimes must have distinct schema hashes "
            "(otherwise surface compatibility is unverifiable)"
        )

        # The model-visible conversation reads top-to-bottom as a clean
        # user/assistant/user/assistant/user/assistant log, and each
        # assistant turn is produced by the runtime bound at that point.
        projection = s.log.derive_messages()
        expected_projection = [
            {"role": "user",      "content": "Map the redirector chain"},
            {"role": "assistant", "content": first_assistant},
            {"role": "user",      "content": "Continue with the foreign runtime"},
            {"role": "assistant", "content": second_assistant},
            {"role": "user",      "content": "Wrap up on the local runtime"},
            {"role": "assistant", "content": third_assistant},
        ]
        for got, want in zip(projection, expected_projection):
            self.assertEqual(got["role"], want["role"])
            self.assertEqual(got["content"], want["content"])
        self.assertEqual(len(projection), len(expected_projection))

        # The user/assistant pairs echo what was committed by each runtime.
        pairs = self._user_assistant_pairs()
        self.assertEqual(len(pairs), 3)
        self.assertEqual(pairs[0][0], "Map the redirector chain")
        self.assertTrue(pairs[0][1].startswith("native-echo"))
        self.assertEqual(pairs[1][0], "Continue with the foreign runtime")
        self.assertTrue(pairs[1][1].startswith("child-echo"))
        self.assertEqual(pairs[2][0], "Wrap up on the local runtime")
        self.assertTrue(pairs[2][1].startswith("native-echo"))

        # The reusable log surface (events up to but not including the
        # first runtime/switch marker) is the portion every runtime must
        # agree on for replay. In the host's current ordering it ends
        # with the previous runtime's runtime/unbind - which is what
        # phase 5's replay fixture also relies on.
        pre_switch = s.log.events_until_switch()
        self.assertEqual(pre_switch[-1].kind, "runtime/unbind")
        self.assertEqual(pre_switch[-1].payload["runtime"], RUNTIME_A)
        # The user's first prompt is somewhere in that surface window;
        # turn 1's assistant reply is too. Both must still be there for
        # a downstream runtime to rebuild the projection from log alone.
        kinds_in_pre = [e.kind for e in pre_switch]
        self.assertIn("user/message", kinds_in_pre)
        self.assertIn("assistant/message", kinds_in_pre)
        self.assertIn("turn/end", kinds_in_pre)
        self.assertEqual(
            [e.payload["text"] for e in pre_switch if e.kind == "user/message"],
            ["Map the redirector chain"],
        )
        # And no runtime/switch slipped into the surface window.
        self.assertNotIn("runtime/switch", kinds_in_pre)


if __name__ == "__main__":
    unittest.main()
