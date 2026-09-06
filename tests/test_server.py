"""Tests for DMHProtocolServer JSON-RPC protocol interface."""

import io
import json
import tempfile
import unittest

from dmh.host import Host
from dmh.native import ScriptedNative
from dmh.server import DMHProtocolServer


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmh-server-")
        self.host = Host(store_dir=self.tmp)
        self.host.register("native", lambda: ScriptedNative(sandbox=self.host.sandbox))

    def tearDown(self):
        self.host.stop_all()

    def rpc_call(self, server, method, params=None, req_id=1):
        line = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        server.dispatch_line(line)

    def test_initialize_rpc(self):
        out = io.StringIO()
        server = DMHProtocolServer(self.host, writer=out)
        self.rpc_call(server, "initialize", req_id=10)

        resp = json.loads(out.getvalue().strip())
        self.assertEqual(resp["id"], 10)
        self.assertEqual(resp["result"]["abiVersion"], 2)
        self.assertIn("native", resp["result"]["supportedRuntimes"])

    def test_session_lifecycle_rpc(self):
        out = io.StringIO()
        server = DMHProtocolServer(self.host, writer=out)

        # 1. session/create
        self.rpc_call(server, "session/create", {"preset": "default", "runtime": "native"}, req_id=1)
        resp1 = json.loads(out.getvalue().strip().split("\n")[0])
        session_id = resp1["result"]["sessionId"]
        self.assertEqual(resp1["result"]["runtime"], "native")

        # 2. session/send
        out.seek(0)
        out.truncate(0)
        self.rpc_call(server, "session/send", {"sessionId": session_id, "text": "Plan architecture"}, req_id=2)
        resp2 = json.loads(out.getvalue().strip().split("\n")[0])
        self.assertTrue(resp2["result"]["ok"])
        self.assertEqual(resp2["result"]["turn"], 1)
        self.assertIn("Plan architecture", resp2["result"]["text"])

        # 3. session/get
        out.seek(0)
        out.truncate(0)
        self.rpc_call(server, "session/get", {"sessionId": session_id}, req_id=3)
        resp3 = json.loads(out.getvalue().strip().split("\n")[0])
        self.assertEqual(resp3["result"]["sessionId"], session_id)
        self.assertEqual(len(resp3["result"]["messages"]), 2)  # user + assistant

        # 4. session/events
        out.seek(0)
        out.truncate(0)
        self.rpc_call(server, "session/events", {"sessionId": session_id, "after": 0}, req_id=4)
        resp4 = json.loads(out.getvalue().strip().split("\n")[0])
        self.assertGreater(len(resp4["result"]), 0)

        # 5. session/list
        out.seek(0)
        out.truncate(0)
        self.rpc_call(server, "session/list", {}, req_id=5)
        resp5 = json.loads(out.getvalue().strip().split("\n")[0])
        self.assertEqual(len(resp5["result"]), 1)

        # 6. session/delete
        out.seek(0)
        out.truncate(0)
        self.rpc_call(server, "session/delete", {"sessionId": session_id}, req_id=6)
        resp6 = json.loads(out.getvalue().strip().split("\n")[0])
        self.assertTrue(resp6["result"]["ok"])


if __name__ == "__main__":
    unittest.main()
