"""Phase 2 - Layer 0/1 handshake conformance (design doc, section 11).

Cookie mismatch, ABI negotiation, stdout pollution, loopback binds, digest
pins, initialize echo, NOT_INITIALIZED, no-reattach.
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dmh import handshake as hs
from dmh.capabilities import HOST_CAPABILITIES
from dmh.child_runtime import ChildRuntime
from dmh.errors import HarnessError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def child_cmd(*extra):
    return [sys.executable, "-B", "-m", "dmh.child", "serve", *extra]


class Phase2HandshakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmh-p2-")

    def spawn(self, env, *extra):
        return subprocess.run(
            child_cmd(*extra), env=env, cwd=ROOT,
            capture_output=True, text=True, timeout=30,
        )

    def test_cookie_mismatch_exits_2_without_handshake_line(self):
        env = hs.child_env(versions=(2, 1), transport="stdio")
        env[hs.MAGIC_COOKIE_KEY] = "wrong-cookie"
        proc = self.spawn(env)
        self.assertEqual(proc.returncode, hs.EXIT_COOKIE)
        self.assertEqual(proc.stdout, "")  # no handshake line, ever
        self.assertIn("MAGIC_COOKIE", proc.stderr)

    def test_no_common_abi_exits_3(self):
        env = hs.child_env(versions=(2,), transport="stdio")
        proc = self.spawn(env, "--supported-abi", "1")
        self.assertEqual(proc.returncode, hs.EXIT_ABI)
        self.assertEqual(proc.stdout, "")
        self.assertIn("ABI major", proc.stderr)

    def test_parse_line_rules(self):
        with self.assertRaises(hs.HandshakeFailure) as ctx:
            hs.parse_handshake_line("1|2|unix", (2,))
        self.assertEqual(ctx.exception.code, "HANDSHAKE_MALFORMED")
        with self.assertRaises(hs.HandshakeFailure) as ctx:
            hs.parse_handshake_line("2|2|unix|/x.sock|jsonrpc", (2,))
        self.assertEqual(ctx.exception.code, "HANDSHAKE_MALFORMED")
        with self.assertRaises(hs.HandshakeFailure) as ctx:
            hs.parse_handshake_line("1|3|unix|/x.sock|jsonrpc", (2,))
        self.assertEqual(ctx.exception.code, "ABI_UNSUPPORTED")
        with self.assertRaises(hs.HandshakeFailure) as ctx:
            hs.parse_handshake_line("1|2|tcp|0.0.0.0:9000|jsonrpc", (2,))
        self.assertEqual(ctx.exception.code, "BIND_NOT_LOOPBACK")
        with self.assertRaises(hs.HandshakeFailure) as ctx:
            hs.parse_handshake_line("1|2|unix|/x.sock|jsonrpc||1|", (2,))
        self.assertEqual(ctx.exception.code, "BROKER_MUX_UNSUPPORTED")
        with self.assertRaises(hs.HandshakeFailure) as ctx:
            hs.parse_handshake_line("1|2|unix|/x.sock|jsonrpc||0|zz", (2,))
        self.assertEqual(ctx.exception.code, "HANDSHAKE_MALFORMED")
        ok = hs.parse_handshake_line("1|2|unix|/tmp/x.sock|jsonrpc||0|", (2, 1))
        self.assertEqual(ok["abi"], 2)



    def test_env_allowlist_blocks_ambient_secrets(self):
        saved = dict(os.environ)
        os.environ["OPENAI_API_KEY"] = "sk-secret"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "hunter2"
        try:
            env = hs.child_env(versions=(2, 1), transport="stdio")
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
            self.assertEqual(env[hs.MAGIC_COOKIE_KEY], hs.MAGIC_COOKIE)
            self.assertEqual(env[hs.PROTOCOL_VERSIONS_KEY], "2,1")
        finally:
            os.environ.clear()
            os.environ.update(saved)
        with self.assertRaises(hs.HandshakeFailure):
            hs.child_env(versions=(2, 1), transport="stdio",
                         spawn_env={"GH_TOKEN": "x"})

    def test_stdout_pollution_detected(self):
        rt = ChildRuntime(
            transport="unix", socket_dir=self.tmp, extra_args=["--pollute"]
        )
        with self.assertRaises(HarnessError) as ctx:
            rt.launch()
        self.assertEqual(ctx.exception.code, "STDOUT_POLLUTION")

    def test_public_tcp_bind_rejected_with_security_finding_code(self):
        rt = ChildRuntime(transport="tcp", extra_args=["--public-bind"])
        with self.assertRaises(HarnessError) as ctx:
            rt.launch()
        self.assertEqual(ctx.exception.code, "BIND_NOT_LOOPBACK")

    def test_two_majors_negotiates_highest_and_echoes(self):
        rt = ChildRuntime(transport="unix", socket_dir=self.tmp, offered=(2, 1))
        facts = rt.launch()
        self.assertEqual(facts["abi"], 2)  # highest common major on the line
        result = rt.initialize(HOST_CAPABILITIES, {})
        self.assertEqual(result["abiVersion"], 2)  # Layer 1 echoes Layer 0
        self.assertTrue(result["runtimeCapabilities"]["streaming"])
        rt.stop()

    def test_digest_mismatch(self):
        rt = ChildRuntime(extra_args=["--digest-override", "0" * 64])
        rt.launch()
        with self.assertRaises(HarnessError) as ctx:
            rt.initialize(HOST_CAPABILITIES, {})
        self.assertEqual(ctx.exception.code, "DIGEST_MISMATCH")
        rt.stop()

    def test_session_method_before_initialize_not_initialized(self):
        rt = ChildRuntime()
        rt.launch()
        with self.assertRaises(HarnessError) as ctx:
            rt.rpc("turn/open", {"epoch": 1, "projection": []})
        self.assertEqual(ctx.exception.code, "NOT_INITIALIZED")
        rt.stop()

    def test_second_initialize_rejected(self):
        rt = ChildRuntime()
        rt.launch()
        rt.initialize(HOST_CAPABILITIES, {})
        with self.assertRaises(HarnessError) as ctx:
            rt.initialize(HOST_CAPABILITIES, {})
        self.assertEqual(ctx.exception.code, "ALREADY_INITIALIZED")
        rt.stop()

    def test_stop_then_initialize_rejected_no_reattach(self):
        rt = ChildRuntime()
        rt.launch()
        rt.initialize(HOST_CAPABILITIES, {})
        rt.stop()
        with self.assertRaises(HarnessError) as ctx:
            rt.initialize(HOST_CAPABILITIES, {})
        self.assertEqual(ctx.exception.code, "RUNTIME_FAULT")

    def test_initialize_omits_capability_means_false(self):
        rt = ChildRuntime(extra_args=["--omit-cap", "tools.native"])
        rt.launch()
        result = rt.initialize(HOST_CAPABILITIES, {})
        caps = result["runtimeCapabilities"]
        self.assertNotIn("tools.native", caps)
        self.assertEqual(rt.capabilities().get("tools.native", False), False)
        self.assertEqual(rt.tool_schemas(), [])
        rt.stop()


if __name__ == "__main__":
    unittest.main()
