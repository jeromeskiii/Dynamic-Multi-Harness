"""Phase 9 - Qwen3.8-Flash-Next foreign runtime: bind, turn, and the fail-closed switch.

Qwen3.8-Flash-Next speaks newline-delimited JSON-RPC over stdio. These tests
drive the REAL protocol server against a stub OpenAI-compatible endpoint, so the
turn path is exercised end-to-end with no network and no model.

All tests skip cleanly when Qwen harness (or its toolchain) is absent.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dmh import qwen_runtime
from dmh.qwen_runtime import QWEN_CAPABILITIES, QwenRuntime, register
from dmh.errors import HarnessError
from dmh.host import Host
from dmh.native import ScriptedNative
from tests.stub_openai import StubOpenAI

_has_qwen = qwen_runtime.available()


def build_host(store):
    host = Host(store_dir=store)
    host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
    register(host)
    return host


@unittest.skipUnless(_has_qwen, "Qwen3.8-Flash-Next runtime not present")
class Phase9QwenRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmh-p9-")
        self.stub = StubOpenAI().start()
        self.addCleanup(self.stub.stop)

    def build_runtime(self):
        return QwenRuntime(
            api_base=self.stub.base_url,
            persistence_root=tempfile.mkdtemp(prefix="dmh-p9-qwen-"),
        )

    def test_bind_succeeds_and_declares_fail_closed_caps(self):
        host = build_host(self.tmp)
        host.runtimes["qwen3.8-flash-next"].factory = self.build_runtime
        session = host.create_session("default")
        session.bind("qwen3.8-flash-next")

        self.assertEqual(session.runtime_id, "qwen3.8-flash-next")
        bind = [e for e in session.log.events if e.kind == "runtime/bind"][0]
        caps = bind.payload["caps"]
        for key, value in QWEN_CAPABILITIES.items():
            self.assertEqual(caps.get(key), value, f"cap {key} mismatch")
        self.assertFalse(caps["replay.from_log"])
        self.assertFalse(caps["tools.native"])
        self.assertNotIn("cancel.fuse", caps)
        host.stop_all()

    def test_submit_streams_and_produces_assistant_message(self):
        host = build_host(self.tmp)
        host.runtimes["qwen3.8-flash-next"].factory = self.build_runtime
        session = host.create_session("default")
        session.bind("qwen3.8-flash-next")
        session.submit("hello from the host to qwen")

        text = session.log.last_assistant_text()
        self.assertIn("hello from the host to qwen", text)
        self.assertTrue(text.startswith("stub-echo"))
        chunks = [e for e in session.log.events if e.kind == "assistant/chunk"]
        self.assertGreater(len(chunks), 1, "streaming must surface as chunks")
        turn_end = [e for e in session.log.events if e.kind == "turn/end"][0]
        self.assertEqual(turn_end.payload["status"], "ok")
        host.stop_all()

    def test_switch_into_qwen_fails_closed(self):
        host = build_host(self.tmp)
        host.runtimes["qwen3.8-flash-next"].factory = self.build_runtime
        session = host.create_session("default")
        session.bind("native")
        session.submit("warm up on the native runtime")

        with self.assertRaises(HarnessError) as ctx:
            session.switch_runtime("qwen3.8-flash-next", reason="specialist")
        self.assertEqual(ctx.exception.code, "CAPABILITY_REQUIRED")
        self.assertIn("replay.from_log", str(ctx.exception))
        bind_failed = [e for e in session.log.events if e.kind == "runtime/bind-failed"]
        self.assertTrue(bind_failed, "the refusal must be on the log")
        self.assertEqual(session.runtime_id, "native")
        host.stop_all()

    def test_consecutive_turns_reuse_the_runtime_session(self):
        host = build_host(self.tmp)
        host.runtimes["qwen3.8-flash-next"].factory = self.build_runtime
        session = host.create_session("default")
        session.bind("qwen3.8-flash-next")
        session.submit("first question")
        session.submit("second question")

        self.assertEqual(session.turn_no, 2)
        messages = session.log.derive_messages()
        assistants = [m for m in messages if m["role"] == "assistant"]
        self.assertEqual(len(assistants), 2)
        host.stop_all()

    def test_translate_surfaces_assistant_message_as_chunk(self):
        runtime = self.build_runtime()
        item = runtime._translate({
            "type": "assistant/message",
            "payload": {"text": "fallback text from qwen"},
        })
        self.assertIsNotNone(item)
        self.assertEqual(item.kind, "chunk")
        self.assertEqual(item.payload["text"], "fallback text from qwen")

    def test_stop_terminates_the_child(self):
        runtime = self.build_runtime()
        runtime.launch()
        runtime.initialize({}, {})
        runtime.stop()
        self.assertIsNotNone(runtime.proc.poll())
