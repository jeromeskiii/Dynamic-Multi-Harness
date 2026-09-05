"""Phase 7 - EV1H-007 foreign runtime: bind, submit, switch, capability gates.

All tests skip cleanly when the foreign runtime binary is absent.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dmh import ev1h_runtime
from dmh.ev1h_runtime import EV1H_CAPABILITIES, register
from dmh.errors import HarnessError
from dmh.host import Host
from dmh.native import ScriptedNative

# Single source of truth for "is the foreign runtime here": ev1h_runtime owns
# the resolution rule (args > EV1H_* env > sibling checkout), so the guard
# cannot disagree with the provider about where EV1H-007 lives.
_has_ev1h = ev1h_runtime.available()


def build_host(store):
    host = Host(store_dir=store)
    host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
    register(host)
    return host


@unittest.skipUnless(_has_ev1h, "EV1H-007 foreign runtime not present")
class Phase7EV1HRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmh-p7-")

    def tearDown(self):
        pass

    def test_bind_succeeds_with_correct_runtime_id_and_caps(self):
        host = build_host(self.tmp)
        s = host.create_session("default")
        s.bind("ev1h-007")
        self.assertEqual(s.runtime_id, "ev1h-007")
        bind = [e for e in s.log.events if e.kind == "runtime/bind"][0]
        self.assertEqual(bind.payload["runtime"], "ev1h-007")
        for key, value in EV1H_CAPABILITIES.items():
            self.assertEqual(
                bind.payload["caps"].get(key), value,
                f"cap {key} mismatch in bind event",
            )
        host.stop_all()

    def test_submit_produces_assistant_message(self):
        host = build_host(self.tmp)
        s = host.create_session("default")
        s.bind("ev1h-007")
        s.submit("hello from test")
        msgs = s.log.derive_messages()
        roles = [m["role"] for m in msgs]
        self.assertIn("assistant", roles)
        host.stop_all()

    def test_switch_at_turn_boundary(self):
        host = build_host(self.tmp)
        s = host.create_session("default")
        s.bind("native")
        s.submit("first turn")
        s.switch_runtime("ev1h-007", reason="specialist")
        s.submit("continue on ev1h")
        switch = [e for e in s.log.events if e.kind == "runtime/switch"]
        self.assertEqual(len(switch), 1)
        self.assertEqual(switch[0].payload["to"], "ev1h-007")
        self.assertEqual(switch[0].payload["reason"], "specialist")
        host.stop_all()

    def test_coding_preset_fails_closed(self):
        host = build_host(self.tmp)
        s = host.create_session("coding")
        with self.assertRaises(HarnessError) as ctx:
            s.bind("ev1h-007")
        self.assertEqual(ctx.exception.code, "CAPABILITY_REQUIRED")
        failed = [e for e in s.log.events if e.kind == "runtime/bind-failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].payload["errorCode"], "CAPABILITY_REQUIRED")
        host.stop_all()

    def test_fresh_provider_after_switch(self):
        host = build_host(self.tmp)
        s = host.create_session("default")
        s.bind("ev1h-007")
        s.submit("first")
        s.switch_runtime("native", reason="back")
        launches = [e for e in s.log.events if e.kind == "runtime/launch"]
        self.assertGreaterEqual(len(launches), 2)
        host.stop_all()


if __name__ == "__main__":
    unittest.main()