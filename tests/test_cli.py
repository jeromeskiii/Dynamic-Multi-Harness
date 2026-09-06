"""Tests for DMH CLI."""

import io
import sys
import tempfile
import unittest
from unittest.mock import patch

from dmh.cli import main


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmh-cli-test-")

    def test_cli_doctor(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["doctor"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("=== DMH System Diagnostics ===", out)
        self.assertIn("native", out)
        self.assertIn("child", out)

    def test_cli_run_and_sessions(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["run", "Analyze code structure", "--runtime", "native", "--store", self.tmp])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("[user] Analyze code structure", out)
        self.assertIn("native-echo: Analyze code structure", out)

        # list sessions
        buf2 = io.StringIO()
        with patch("sys.stdout", buf2):
            code2 = main(["sessions", "list", "--store", self.tmp])
        self.assertEqual(code2, 0)
        out2 = buf2.getvalue()
        self.assertIn("native", out2)

    def test_cli_sessions_verify(self):
        # Run a task to create session
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            main(["run", "Task for verify", "--runtime", "native", "--store", self.tmp])

        # Get session ID
        from dmh.persistence import SessionStore
        store = SessionStore(self.tmp)
        sessions = store.list_sessions()
        self.assertEqual(len(sessions), 1)
        sid = sessions[0].session_id

        # Verify session
        buf2 = io.StringIO()
        with patch("sys.stdout", buf2):
            code = main(["sessions", "verify", sid, "--store", self.tmp])
        self.assertEqual(code, 0)
        self.assertIn("integrity: OK", buf2.getvalue())

    def test_cli_help(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main([])
        self.assertEqual(code, 0)
        self.assertIn("Dynamic Multi-Harness", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
