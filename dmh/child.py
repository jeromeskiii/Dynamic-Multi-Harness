"""The foreign harness runtime binary (topology B backend).

Serve the harness ABI over stdio or a loopback socket:
    python -m dmh.child serve [--omit-cap CAP] [--supported-abi 2,1]

Layer 0: magic cookie gate, ABI selection, one stdout handshake line
(unix/tcp only; stdio skips the line). Layer 1: NDJSON JSON-RPC 2.0,
initialize first, always. stdout is frames only; logs go to stderr.

This is a distinct runtime with its own private loop state machine - the
point of the exercise is that the host can swap it mid-session.
"""

import argparse
import json
import os
import socket
import sys
import tempfile
import time
import uuid

from . import wire
from . import handshake as hs
from .errors import HarnessError
from .native import split_chunks
from .provider import compute_schema_hash

CHILD_CAPABILITIES = {
    "streaming": True,
    "tools.native": True,
    "tools.code": True,
    "plan": True,
    "goals": False,
    "mcp.client": True,
    "approval.ask": True,
    "compaction": True,
    "replay.from_log": True,
    "subagents.native": False,
    "prompt.image": False,
}


class ChildRuntime:
    """Private state machine: boot -> initialized -> configured -> turn.

    This state is harness-owned and never shared with the host; if it is
    lost, the host can resume the session from the log projection alone.
    """

    def __init__(self, args, abi, caps, digest, reader, writer):
        self.args = args
        self.abi = abi
        self.caps = dict(caps)
        self.digest = digest
        self.reader = reader
        self.writer = writer
        self.initialized = False
        self.projection = []
        self.epoch = 0
        self.cancel_requested = False
        self.script = None
        raw = os.environ.get("HARNESS_CHILD_SCRIPT")
        if raw:
            self.script = json.loads(raw)
        self.idx = 0
        self.turn_step = 0
        self._rpc = 9000

    # ---- serve loop ----
    def serve(self):
        while True:
            line = self.reader.readline()
            if line is None:
                continue
            if not line.strip():
                continue
            frame = wire.loads(line)
            if wire.is_request(frame):
                if self.handle_request(frame) == "STOP":
                    return hs.EXIT_OK
            # responses/notifications unsolicited by the host are ignored

    def handle_request(self, frame):
        method = frame.get("method")
        params = frame.get("params") or {}
        req_id = frame["id"]
        try:
            if method == "initialize":
                result = self.on_initialize(params)
            else:
                if not self.initialized:
                    raise HarnessError(
                        "NOT_INITIALIZED", f"{method} called before initialize"
                    )
                if method == "auth/login":
                    result = {}
                elif method == "configure":
                    result = self.on_configure(params)
                elif method == "turn/open":
                    result = self.on_turn_open(params)
                elif method == "turn/cancel":
                    self.cancel_requested = True
                    result = {}
                elif method == "step/run":
                    result = self.on_step(params)
                elif method == "shutdown":
                    self.writer.send(wire.response(req_id, {}))
                    return "STOP"
                else:
                    raise HarnessError("METHOD_NOT_FOUND", f"unknown method {method}")
            self.writer.send(wire.response(req_id, result))
            return None
        except HarnessError as exc:
            self.writer.send(
                wire.error_response(req_id, exc.code, exc.message, exc.data)
            )
            return None



    # ---- Layer 1 handlers ----
    def on_initialize(self, params):
        if self.initialized:
            raise HarnessError("ALREADY_INITIALIZED", "initialize already completed")
        abi = params.get("abiVersion")
        if abi != self.abi:
            raise HarnessError(
                "ABI_MISMATCH",
                f"layer-1 abi {abi} != negotiated abi {self.abi}",
                {"requested": abi, "served": self.abi},
            )
        abi_min = params.get("abiVersionMin")
        if abi_min is not None and self.abi < abi_min:
            raise HarnessError("ABI_TOO_OLD", f"abi {self.abi} below min {abi_min}")
        self.initialized = True
        return {
            "abiVersion": self.abi,
            "runtimeInfo": {
                "name": "child-loop",
                "version": "0.1.0",
                "vendor": "foreign",
                "digest": self.digest,
            },
            "runtimeCapabilities": dict(self.caps),
            "authMethods": [],
            "schemaHash": compute_schema_hash(
                "child", self.caps,
                ["fs.read"] if self.caps.get("tools.native") else [],
            ),
        }

    def on_configure(self, params):
        self.snapshot = dict((params.get("snapshot") or {}))
        return {}

    def on_turn_open(self, params):
        self.epoch = params.get("epoch", 0)
        self.projection = list(params.get("projection") or [])
        self.turn_step = 0
        return {}

    def on_step(self, params):
        resume = params.get("resume")
        if self.cancel_requested:
            # cancellation before dispatch appends ABORTED_BEFORE_DISPATCH
            self.cancel_requested = False
            return {"turn_end": False, "aborted": "before_dispatch"}
        self.turn_step += 1
        action = self._next_action()
        if action is None:
            return {"turn_end": True, "aborted": None}
        if "fail" in action:
            return {"turn_end": False, "aborted": None, "error": str(action["fail"])}
        completed = self._emit_action(action, resume)
        if completed is False or self.cancel_requested:
            # cancellation after body start drains to quiescence -> ABORTED
            self.cancel_requested = False
            return {"turn_end": False, "aborted": "drained"}
        return {"turn_end": bool(action.get("end")), "aborted": None}



    # ---- scripted behavior (harness-private) ----
    def _next_action(self):
        if self.script is not None:
            if self.idx >= len(self.script):
                return None
            action = self.script[self.idx]
            self.idx += 1
            return action
        if self.turn_step == 1:
            return {"say": "child-echo: {user} (context={context})"}
        return {"end": True}

    def _emit_action(self, action, resume):
        if "tool" in action:
            tool = action["tool"]
            self.writer.send(wire.notification("step/tool_call", {
                "name": tool.get("name"),
                "args": dict(tool.get("args") or {}),
                "approval_required": bool(tool.get("approval_required")),
            }))
            return True
        text = None
        delay = 0.0
        if "say" in action:
            text = self._fmt(action["say"], resume)
        elif "slow_say" in action:
            text = self._fmt(action["slow_say"], resume)
            delay = float(action.get("delay") or self.args.slow or 0.0)
        elif "say_result" in action:
            text = self._fmt(action["say_result"], resume)
        elif "host_read" in action:
            result = self.host_request(
                "host/fs_read", {"path": action["host_read"].get("path", "")}
            )
            text = (
                result.get("content")
                if result.get("ok") else f"denied: {result.get('content')}"
            )
        elif "end" in action:
            return True
        else:
            raise HarnessError("RUNTIME_FAULT", f"unknown child action {action!r}")
        return self._emit_chunks(text, delay)

    def _emit_chunks(self, text, delay):
        for chunk in split_chunks(text):
            if delay:
                time.sleep(delay)
            if self._poll_host_requests():
                return False  # cancel drained us
            self.writer.send(wire.notification("step/chunk", {"text": chunk}))
        return True

    def _poll_host_requests(self):
        """Between chunks, observe a fused abort: a blocked write must still
        notice turn/cancel."""
        cancelled = False
        while True:
            line = self.reader.readline(timeout=0)
            if line is None:
                return cancelled
            if not line.strip():
                continue
            frame = wire.loads(line)
            if wire.is_request(frame):
                if frame.get("method") == "turn/cancel":
                    self.cancel_requested = True
                    cancelled = True
                    self.writer.send(wire.response(frame["id"], {}))
                else:
                    self.handle_request(frame)

    def host_request(self, method, params):
        """Child-initiated callback on the same stream (mux-by-message)."""
        self._rpc += 1
        req_id = self._rpc
        self.writer.send(wire.request(req_id, method, params))
        while True:
            line = self.reader.readline(timeout=10.0)
            if line is None:
                raise HarnessError("RUNTIME_FAULT", f"host service {method} timed out")
            if not line.strip():
                continue
            frame = wire.loads(line)
            if wire.is_request(frame):
                self.handle_request(frame)
                continue
            if wire.is_response(frame) and frame.get("id") == req_id:
                if "error" in frame:
                    err = frame["error"]
                    data = err.get("data") or {}
                    raise HarnessError(
                        data.get("errorCode", "RUNTIME_FAULT"),
                        err.get("message", "host service error"),
                    )
                return frame.get("result") or {}

    def _fmt(self, text, resume):
        return (
            text.replace("{user}", self._last_user())
            .replace("{context}", str(len(self.projection)))
            .replace("{result}", str((resume or {}).get("content", "")))
        )

    def _last_user(self):
        for m in reversed(self.projection):
            if m.get("role") == "user":
                return m.get("content", "")
        return ""


# ---- entry point ----

def _make_runtime(args, abi, caps, digest, reader, writer):
    return ChildRuntime(args, abi, caps, digest, reader, writer)


def run_stdio(runtime):
    try:
        runtime.serve()
    except EOFError:
        pass
    return hs.EXIT_OK


def run_socket(runtime, conn, srv, path):
    try:
        runtime.serve()
    except EOFError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
        try:
            srv.close()
        except OSError:
            pass
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    return hs.EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dmh.child")
    ap.add_argument("command", choices=["serve"])
    ap.add_argument("--omit-cap", action="append", default=[])
    ap.add_argument("--supported-abi", default="2,1")
    ap.add_argument("--pollute", action="store_true")
    ap.add_argument("--digest-override", default=None)
    ap.add_argument("--public-bind", action="store_true")
    ap.add_argument("--slow", type=float, default=0.0)
    args = ap.parse_args(argv)

    # Layer 0 gate 1: cookie. Wrong/missing cookie = stderr message, exit 2,
    # and crucially NO handshake line on stdout.
    cookie = os.environ.get(hs.MAGIC_COOKIE_KEY)
    if cookie != hs.MAGIC_COOKIE:
        sys.stderr.write(
            "dmh.child: bad or missing HARNESS_PLUGIN_MAGIC_COOKIE; this "
            "binary is only launched by a dmh host. No handshake line.\n"
        )
        return hs.EXIT_COOKIE

    # Layer 0 gate 2: ABI selection. No overlap = stderr diagnosis, exit 3.
    offered = [
        int(x) for x in (os.environ.get(hs.PROTOCOL_VERSIONS_KEY) or "2,1").split(",")
        if x.strip()
    ]
    supported = [int(x) for x in args.supported_abi.split(",") if x.strip()]
    abi = hs.select_abi(offered, supported)
    if abi is None:
        sys.stderr.write(
            f"dmh.child: no overlapping ABI major "
            f"(host offered {offered}, child supports {supported}).\n"
        )
        return hs.EXIT_ABI

    transport = os.environ.get(hs.TRANSPORT_KEY, "stdio")
    digest = args.digest_override or hs.sha256_file(__file__)
    caps = {k: v for k, v in CHILD_CAPABILITIES.items() if k not in args.omit_cap}

    if transport == "stdio":
        runtime = _make_runtime(
            args, abi, caps, digest, wire.LineReader(0), wire.LineWriter(1)
        )
        return run_stdio(runtime)

    if transport == "unix":
        sock_dir = os.environ.get(hs.UNIX_SOCKET_DIR_KEY) or tempfile.gettempdir()
        path = os.path.join(sock_dir, f"dmh-{uuid.uuid4().hex[:8]}.sock")
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        try:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(path)
            os.chmod(path, 0o600)
            srv.listen(1)
        except OSError as exc:
            sys.stderr.write(f"dmh.child: cannot bind unix socket: {exc}\n")
            return hs.EXIT_BIND
        if args.pollute:
            os.write(1, b"child log line - definitely not a handshake\n")
        os.write(1, (hs.build_handshake_line(abi, "unix", path, "jsonrpc", "", 0, digest) + "\n").encode("utf-8"))
        conn, _ = srv.accept()
        runtime = _make_runtime(
            args, abi, caps, digest,
            wire.LineReader(conn.fileno()), wire.LineWriter(conn.fileno()),
        )
        return run_socket(runtime, conn, srv, path)

    if transport == "tcp":
        host = "0.0.0.0" if args.public_bind else "127.0.0.1"
        min_port = int(os.environ.get(hs.MIN_PORT_KEY) or 0)
        max_port = int(os.environ.get(hs.MAX_PORT_KEY) or 0)
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if min_port:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                for port in range(min_port, max_port + 1):
                    try:
                        srv.bind((host, port))
                        break
                    except OSError:
                        continue
                else:
                    raise OSError("no free port in range")
            else:
                srv.bind((host, 0))
            srv.listen(1)
            port = srv.getsockname()[1]
        except OSError as exc:
            sys.stderr.write(f"dmh.child: cannot bind tcp socket: {exc}\n")
            return hs.EXIT_BIND
        if args.pollute:
            os.write(1, b"child log line - definitely not a handshake\n")
        os.write(1, (hs.build_handshake_line(abi, "tcp", f"{host}:{port}", "jsonrpc", "", 0, digest) + "\n").encode("utf-8"))
        conn, _ = srv.accept()
        runtime = _make_runtime(
            args, abi, caps, digest,
            wire.LineReader(conn.fileno()), wire.LineWriter(conn.fileno()),
        )
        return run_socket(runtime, conn, srv, None)

    sys.stderr.write(f"dmh.child: unknown transport {transport!r}\n")
    return hs.EXIT_BOOT


if __name__ == "__main__":
    sys.exit(main())
