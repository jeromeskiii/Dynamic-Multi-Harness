"""Phase 4 - Delegation and the approval seam: child sessions with lineage,
depth guard, host-owned approval decisions, unknown-tool denial, reserved
transport for host fs."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dmh.child_runtime import ChildRuntime
from dmh.host import AllowAll, DenyAll, Host
from dmh.native import ScriptedNative


def build_host(store, policy=None):
    host = Host(store_dir=store, approval_policy=policy)
    host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
    host.register("child", lambda: ChildRuntime(sandbox=host.sandbox))
    return host


TOOL_SCRIPT = [
    {"tool": {"name": "fs.read", "args": {"path": "notes.txt"}, "approval_required": True}},
    {"say_result": "tool said: {result}"},
    {"end": True},
]


class Phase4DelegateApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmh-p4-")

    def test_delegate_creates_child_session_with_lineage_depth(self):
        host = build_host(self.tmp)
        host.sandbox.write("notes.txt", "delegate-note")
        s = host.create_session("default")
        s.bind("native")
        child = s.delegate_harness("native", "read the notes and echo")
        self.assertIsNotNone(child)
        self.assertEqual(child.parent_id, s.id)
        self.assertEqual(child.delegation_depth, 1)
        tools = [e for e in s.log.events if e.kind == "tool/call"]
        results = [e for e in s.log.events if e.kind == "tool/result"]
        self.assertEqual(tools[0].payload["tool"], "delegate_harness")
        self.assertEqual(results[0].payload["child_session"], child.id)
        self.assertIn("session/delegate", [e.kind for e in s.log.events])
        host.stop_all()

    def test_delegate_depth_guard_refuses_grandchild(self):
        host = build_host(self.tmp)
        s = host.create_session("default")
        s.bind("native")
        child = s.delegate_harness("native", "level 1")
        grandchild = child.delegate_harness("native", "level 2")
        self.assertIsNotNone(grandchild)
        self.assertEqual(grandchild.delegation_depth, 2)
        refused = grandchild.delegate_harness("native", "level 3")
        self.assertIsNone(refused)
        results = [e for e in grandchild.log.events if e.kind == "tool/result"]
        self.assertEqual(results[0].payload["status"], "refused")
        self.assertEqual(results[0].payload["content"], "DELEGATION_DEPTH_EXCEEDED")
        host.stop_all()

    def test_approval_is_host_seam_deny_all(self):
        host = build_host(self.tmp, policy=DenyAll())
        host.sandbox.write("notes.txt", "secret-note")
        host.register(
            "tooler",
            lambda: ChildRuntime(sandbox=host.sandbox, script=TOOL_SCRIPT),
        )
        s = host.create_session("default")
        s.bind("tooler")
        s.submit("read it")
        kinds = [e.kind for e in s.log.events]
        self.assertIn("approval/request", kinds)
        self.assertIn("approval/decided", kinds)
        decided = [e for e in s.log.events if e.kind == "approval/decided"][0]
        self.assertFalse(decided.payload["approved"])
        result = [e for e in s.log.events if e.kind == "tool/result"][0]
        self.assertEqual(result.payload["status"], "denied")
        self.assertNotIn("secret-note", s.log.last_assistant_text())
        host.stop_all()

    def test_approval_allow_all_reads_sandbox(self):
        host = build_host(self.tmp, policy=AllowAll())
        host.sandbox.write("notes.txt", "secret-note")
        host.register(
            "tooler",
            lambda: ChildRuntime(sandbox=host.sandbox, script=TOOL_SCRIPT),
        )
        s = host.create_session("default")
        s.bind("tooler")
        s.submit("read it")
        result = [e for e in s.log.events if e.kind == "tool/result"][0]
        self.assertEqual(result.payload["status"], "ok")
        self.assertIn("secret-note", result.payload["content"])
        self.assertIn("secret-note", s.log.last_assistant_text())
        host.stop_all()

    def test_unknown_tool_denied_even_when_approved(self):
        host = build_host(self.tmp, policy=AllowAll())
        host.register(
            "tooler",
            lambda: ScriptedNative(
                sandbox=host.sandbox,
                script=[{"tool": {"name": "shell.exec", "args": {}}},
                        {"end": True}],
            ),
        )
        s = host.create_session("default")
        s.bind("tooler")
        s.submit("run it")
        result = [e for e in s.log.events if e.kind == "tool/result"][0]
        self.assertEqual(result.payload["status"], "denied")
        self.assertIn("unknown tool", result.payload["content"])
        host.stop_all()

    def test_child_reaches_host_fs_only_via_reserved_transport(self):
        host = build_host(self.tmp)
        host.sandbox.write("notes.txt", "via-reserved-transport")
        host.register(
            "reader",
            lambda: ChildRuntime(
                sandbox=host.sandbox,
                script=[{"host_read": {"path": "notes.txt"}}, {"end": True}],
            ),
        )
        s = host.create_session("default")
        s.bind("reader")
        s.submit("read")
        self.assertIn("via-reserved-transport", s.log.last_assistant_text())
        # traversal outside the sandbox root is denied
        host.register(
            "escapee",
            lambda: ChildRuntime(
                sandbox=host.sandbox,
                script=[{"host_read": {"path": "../etc/passwd"}}, {"end": True}],
            ),
        )
        s2 = host.create_session("default")
        s2.bind("escapee")
        s2.submit("escape")
        self.assertIn("denied:", s2.log.last_assistant_text())
        host.stop_all()


if __name__ == "__main__":
    unittest.main()
