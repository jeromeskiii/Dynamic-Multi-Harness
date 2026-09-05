"""Phase 8 - cognihak foreign runtime: bind, turn, and the fail-closed switch.

cognihak speaks its own JSON-RPC dialect, so its provider is a translator,
not a DMH-ABI child. These tests drive the REAL protocol server (spawned the
way the provider spawns it) against a stub OpenAI-compatible endpoint, so the
turn path is exercised end-to-end with no network and no model.

All tests skip cleanly when cognihak (or its toolchain) is absent.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dmh import cognihak_runtime
from dmh.cognihak_runtime import COGNIHAK_CAPABILITIES, CognihakRuntime, register
from dmh.errors import HarnessError
from dmh.host import Host
from dmh.native import ScriptedNative
from tests.stub_openai import StubOpenAI

_has_cognihak = cognihak_runtime.available()


def build_host(store):
    host = Host(store_dir=store)
    host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
    register(host)
    return host


@unittest.skipUnless(_has_cognihak, "cognihak runtime not present")
class Phase8CognihakRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmh-p8-")
        self.stub = StubOpenAI().start()
        self.addCleanup(self.stub.stop)

    def build_runtime(self):
        return CognihakRuntime(
            api_base=self.stub.base_url,
            persistence_root=tempfile.mkdtemp(prefix="dmh-p8-cog-"),
        )

    def test_bind_succeeds_and_declares_fail_closed_caps(self):
        host = build_host(self.tmp)
        host.runtimes["cognihak"].factory = self.build_runtime
        session = host.create_session("default")
        session.bind("cognihak")

        self.assertEqual(session.runtime_id, "cognihak")
        bind = [e for e in session.log.events if e.kind == "runtime/bind"][0]
        caps = bind.payload["caps"]
        for key, value in COGNIHAK_CAPABILITIES.items():
            self.assertEqual(caps.get(key), value, f"cap {key} mismatch")
        # the two claims that matter, spelled out
        self.assertFalse(caps["replay.from_log"])
        self.assertFalse(caps["tools.native"])
        self.assertNotIn("cancel.fuse", caps)
        host.stop_all()

    def test_submit_streams_and_produces_assistant_message(self):
        host = build_host(self.tmp)
        host.runtimes["cognihak"].factory = self.build_runtime
        session = host.create_session("default")
        session.bind("cognihak")
        session.submit("hello from the host")

        text = session.log.last_assistant_text()
        self.assertIn("hello from the host", text)
        self.assertTrue(text.startswith("stub-echo"))
        chunks = [e for e in session.log.events if e.kind == "assistant/chunk"]
        self.assertGreater(len(chunks), 1, "streaming must surface as chunks")
        turn_end = [e for e in session.log.events if e.kind == "turn/end"][0]
        self.assertEqual(turn_end.payload["status"], "ok")
        host.stop_all()

    def test_switch_into_cognihak_fails_closed(self):
        host = build_host(self.tmp)
        host.runtimes["cognihak"].factory = self.build_runtime
        session = host.create_session("default")
        session.bind("native")
        session.submit("warm up on the native runtime")

        with self.assertRaises(HarnessError) as ctx:
            session.switch_runtime("cognihak", reason="specialist")
        self.assertEqual(ctx.exception.code, "CAPABILITY_REQUIRED")
        self.assertIn("replay.from_log", str(ctx.exception))
        bind_failed = [e for e in session.log.events if e.kind == "runtime/bind-failed"]
        self.assertTrue(bind_failed, "the refusal must be on the log")
        # the session must survive the failed switch, still on native
        self.assertEqual(session.runtime_id, "native")
        host.stop_all()

    def test_consecutive_turns_reuse_the_runtime_session(self):
        host = build_host(self.tmp)
        host.runtimes["cognihak"].factory = self.build_runtime
        session = host.create_session("default")
        session.bind("cognihak")
        session.submit("first question")
        session.submit("second question")

        self.assertEqual(session.turn_no, 2)
        messages = session.log.derive_messages()
        assistants = [m for m in messages if m["role"] == "assistant"]
        self.assertEqual(len(assistants), 2)
        host.stop_all()

    def test_translate_surfaces_assistant_message_as_chunk(self):
        """Regression: cognihak may emit assistant/message directly (no
        preceding assistant/chunk events - e.g. summarization, tool-only
        compressed responses). Until _translate is taught to handle that
        event type, the host silently loses the text and derive_messages()
        sees no assistant content."""
        runtime = self.build_runtime()
        item = runtime._translate({
            "type": "assistant/message",
            "payload": {"text": "fallback text"},
        })
        self.assertIsNotNone(
            item, "assistant/message must surface; today _translate returns None"
        )
        self.assertEqual(item.kind, "chunk")
        self.assertEqual(item.payload["text"], "fallback text")

    def test_stop_terminates_the_child(self):
        runtime = self.build_runtime()
        runtime.launch()
        runtime.initialize({}, {})
        runtime.stop()
        # the child must be gone after stop(), not just detached
        self.assertIsNotNone(runtime.proc.poll())
