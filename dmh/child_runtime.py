"""Host-side adapter: spawn a foreign harness child and speak the ABI.

One spawn per bind: no reattach (single-use, like go-plugin AutoMTLS).
Layer 0 for unix/tcp transports, Layer 1 initialize always. The host owns
capability intersection, approval, cancellation and the session log.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import wire
from . import handshake as hs
from .capabilities import normalize
from .errors import HarnessError
from .provider import HarnessProvider, StreamItem

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
CHILD_MODULE = str(Path(__file__).resolve().parent / "child.py")
STEP_TIMEOUT = 30.0


class ChildRuntime(HarnessProvider):
    """A spawned runtime child; the foreign harness as a provider."""

    runtime_id = "child"

    def __init__(self, *, transport="stdio", offered=(2, 1), socket_dir=None,
                 extra_args=(), env_allow=(), spawn_env=None, script=None,
                 sandbox=None, expected_digest=None, min_port=None, max_port=None):
        self.transport = transport
        self.offered = tuple(int(v) for v in offered)
        self.socket_dir = socket_dir
        self.extra_args = list(extra_args)
        self.env_allow = list(env_allow)
        self.spawn_env = dict(spawn_env or {})
        if script is not None:
            import json
            self.spawn_env["HARNESS_CHILD_SCRIPT"] = json.dumps(script)
        self.sandbox = sandbox
        self.expected_digest = expected_digest
        self.min_port = min_port
        self.max_port = max_port
        self.proc = None
        self.conn = None
        self.reader = None
        self.writer = None
        self._rpc = 0
        self._caps = {}
        self._abi = None
        self._handshake = None
        self._stopped = False
        self._stash = []
        self._host_services = {"host/fs_read": self._svc_fs_read}

    # ---- overridable spawn/digest hooks (subclass to change runtime) ----
    def spawn_argv(self):
        return [sys.executable, "-B", "-m", "dmh.child", "serve", *self.extra_args]

    def spawn_cwd(self):
        return PROJECT_ROOT

    def digest_path(self):
        return CHILD_MODULE



    # ---- lifecycle ----
    def launch(self):
        if self.proc is not None:
            raise HarnessError(
                "RUNTIME_FAULT", "child already launched (reattach is forbidden)"
            )
        try:
            env = hs.child_env(
                versions=self.offered, transport=self.transport,
                socket_dir=self.socket_dir, allow=self.env_allow,
                spawn_env=self.spawn_env,
                min_port=self.min_port, max_port=self.max_port,
            )
        except hs.HandshakeFailure as exc:
            raise HarnessError(exc.code, exc.message) from None
        argv = self.spawn_argv()
        self.proc = subprocess.Popen(
            argv, env=env, cwd=self.spawn_cwd(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        facts = {
            "runtime_id": self.runtime_id,
            "argv0": argv[0],
            "transport": self.transport,
            "instance": env[hs.INSTANCE_ID_KEY],
        }
        if self.transport == "stdio":
            self.reader = wire.LineReader(self.proc.stdout.fileno())
            self.writer = wire.LineWriter(self.proc.stdin.fileno())
            self._check_boot_exit()
            return facts
        # Layer 0 for socket transports
        out_reader = wire.LineReader(self.proc.stdout.fileno())
        line = out_reader.readline(timeout=hs.HANDSHAKE_TIMEOUT)
        if line is None:
            self._kill()
            raise HarnessError(
                "HANDSHAKE_TIMEOUT", "no handshake line within budget"
            )
        self._check_boot_exit()
        text = line.decode("utf-8", "replace").rstrip("\n")
        if "|" not in text:
            self._kill()
            raise HarnessError(
                "STDOUT_POLLUTION",
                f"first stdout line is not a handshake line: {text!r}",
                {"line": hs.redact(text, 200)},
            )
        try:
            hsinfo = hs.parse_handshake_line(
                text, self.offered, min_port=self.min_port, max_port=self.max_port
            )
        except hs.HandshakeFailure as exc:
            self._kill()
            raise HarnessError(
                exc.code, exc.message, {"line": hs.redact(text, 200)}
            ) from None
        self._handshake = hsinfo
        self._abi = hsinfo["abi"]
        self.conn = (
            self._connect_unix(hsinfo["addr"])
            if self.transport == "unix" else self._connect_tcp(hsinfo["addr"])
        )
        self.reader = wire.LineReader(self.conn.fileno())
        self.writer = wire.LineWriter(self.conn.fileno())
        facts.update({
            "abi": hsinfo["abi"], "net": hsinfo["net"], "addr": hsinfo["addr"],
            "wire": hsinfo["wire"], "digest": hsinfo["digest"] or None,
        })
        return facts



    def _check_boot_exit(self):
        code = self.proc.poll()
        if code is None:
            return
        tail = self._stderr_tail()
        if code == hs.EXIT_COOKIE:
            raise HarnessError(
                "RUNTIME_FAULT", "child rejected the magic cookie", {"stderr_tail": tail}
            )
        if code == hs.EXIT_ABI:
            raise HarnessError(
                "ABI_UNSUPPORTED",
                f"no overlapping ABI major (child exited {code})",
                {"stderr_tail": tail},
            )
        raise HarnessError(
            "RUNTIME_FAULT", f"child exited {code} during boot", {"stderr_tail": tail}
        )

    def _stderr_tail(self):
        if self.proc is None or self.proc.stderr is None:
            return ""
        try:
            data = self.proc.stderr.read()
        except OSError:
            return ""
        return hs.redact(data.decode("utf-8", "replace"))

    def _connect_unix(self, addr):
        deadline = time.monotonic() + hs.HANDSHAKE_TIMEOUT
        while time.monotonic() < deadline:
            if os.path.exists(addr):
                break
            time.sleep(0.02)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(hs.HANDSHAKE_TIMEOUT)
        sock.connect(addr)
        sock.settimeout(None)
        return sock

    def _connect_tcp(self, addr):
        host, _, port_s = addr.rpartition(":")
        return socket.create_connection(
            (host.strip("[]"), int(port_s)), timeout=hs.HANDSHAKE_TIMEOUT
        )

    def _kill(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._close_streams()

    # ---- JSON-RPC over the child stream ----
    def rpc(self, method, params=None, timeout=10.0, timeout_code="RUNTIME_FAULT"):
        self._rpc += 1
        req_id = self._rpc
        self.writer.send(wire.request(req_id, method, params))
        return self._await(req_id, timeout, timeout_code)

    def _await(self, req_id, timeout, timeout_code):
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            line = self.reader.readline(timeout=remaining)
            if line is None:
                self._kill()
                raise HarnessError(timeout_code, f"timed out waiting for response id={req_id}")
            if not line.strip():
                continue
            frame = wire.loads(line)
            if wire.is_request(frame):
                self._serve_child_request(frame)
                continue
            if wire.is_response(frame):
                if frame.get("id") != req_id:
                    self._stash.append(frame)  # stray ack, e.g. turn/cancel
                    continue
                if "error" in frame:
                    err = frame["error"]
                    data = err.get("data") or {}
                    raise HarnessError(
                        data.get("errorCode", "RUNTIME_FAULT"),
                        err.get("message", "child error"),
                        data,
                    )
                return frame.get("result") or {}
            if wire.is_notification(frame):
                self._stash.append(frame)
                continue

    def _serve_child_request(self, frame):
        method = frame.get("method")
        params = frame.get("params") or {}
        handler = self._host_services.get(method)
        if handler is None:
            self.writer.send(wire.error_response(
                frame["id"], "METHOD_NOT_FOUND", f"no host service {method}"
            ))
            return
        try:
            self.writer.send(wire.response(frame["id"], handler(params)))
        except HarnessError as exc:
            self.writer.send(wire.error_response(
                frame["id"], exc.code, exc.message, exc.data
            ))

    def _svc_fs_read(self, params):
        if self.sandbox is None:
            return {"ok": False, "content": "no sandbox world"}
        ok, content = self.sandbox.fs_read(params.get("path", ""))
        return {"ok": ok, "content": content}



    # ---- provider contract ----
    def capabilities(self):
        return dict(self._caps)

    def initialize(self, host_caps, constraints):
        if self._stopped or self.proc is None or self.proc.poll() is not None:
            raise HarnessError(
                "RUNTIME_FAULT", "child process is gone; new spawn, new handshake"
            )
        if self._abi is None:
            self._abi = max(self.offered)
        params = {
            "abiVersion": self._abi,
            "abiVersionMin": min(self.offered),
            "hostInfo": {"name": "dmh-host", "version": "0.1.0", "instanceId": "host"},
            "hostCapabilities": dict(host_caps or {}),
            "constraints": dict(constraints or {}),
        }
        result = self.rpc(
            "initialize", params,
            timeout=hs.INITIALIZE_TIMEOUT, timeout_code="INITIALIZE_TIMEOUT",
        )
        abi = result.get("abiVersion")
        if self._handshake is not None and abi != self._handshake["abi"]:
            raise HarnessError(
                "ABI_MISMATCH",
                f"layer-1 abi {abi} != layer-0 abi {self._handshake['abi']}",
            )
        if abi not in self.offered:
            raise HarnessError(
                "ABI_UNSUPPORTED", f"runtime serves abi {abi}, host offered {list(self.offered)}"
            )
        digest = (result.get("runtimeInfo") or {}).get("digest")
        expected = self.expected_digest or hs.sha256_file(self.digest_path())
        if digest and digest != expected:
            raise HarnessError(
                "DIGEST_MISMATCH",
                "reported digest != hashed file on disk",
                {"reported": digest[:16], "expected": expected[:16]},
            )
        self._caps = normalize(result.get("runtimeCapabilities"))
        return result

    def configure(self, snapshot):
        self.rpc(
            "configure", {"snapshot": dict(snapshot or {})},
            timeout=hs.CONFIGURE_TIMEOUT, timeout_code="CONFIGURE_TIMEOUT",
        )

    def open_turn(self, epoch, projection):
        self.rpc("turn/open", {"epoch": epoch, "projection": list(projection or [])})



    def run_step(self, resume=None):
        params = {"resume": resume} if resume else {}
        self._rpc += 1
        req_id = self._rpc
        self.writer.send(wire.request(req_id, "step/run", params))
        deadline = time.monotonic() + STEP_TIMEOUT
        while True:
            if self._stash:
                frame = self._stash.pop(0)
            else:
                remaining = max(0.0, deadline - time.monotonic())
                line = self.reader.readline(timeout=remaining)
                if line is None:
                    self._kill()
                    raise HarnessError("RUNTIME_FAULT", "step stream timed out")
                if not line.strip():
                    continue
                frame = wire.loads(line)
            if wire.is_notification(frame):
                method = frame.get("method")
                payload = frame.get("params") or {}
                if method == "step/chunk":
                    yield StreamItem("chunk", {"text": payload.get("text", "")})
                elif method == "step/tool_call":
                    yield StreamItem("tool_call", {
                        "name": payload.get("name"),
                        "args": dict(payload.get("args") or {}),
                        "approval_required": bool(payload.get("approval_required")),
                    })
                continue
            if wire.is_request(frame):
                self._serve_child_request(frame)
                continue
            if wire.is_response(frame) and frame.get("id") == req_id:
                if "error" in frame:
                    err = frame["error"]
                    data = err.get("data") or {}
                    raise HarnessError(
                        data.get("errorCode", "RUNTIME_FAULT"),
                        err.get("message", "child step error"),
                        data,
                    )
                result = frame.get("result") or {}
                yield StreamItem("step_end", {
                    "turn_end": bool(result.get("turn_end")),
                    "aborted": result.get("aborted"),
                    "error": result.get("error"),
                })
                return
            # stray response: ignore

    def cancel(self):
        if self.proc is None or self.proc.poll() is not None or self._stopped:
            return
        try:
            self.rpc("turn/cancel", {}, timeout=5.0)
        except HarnessError:
            pass  # drain to quiescence; the host stays alive

    def stop(self):
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None and not self._stopped:
                try:
                    self.rpc("shutdown", {}, timeout=hs.STOP_TIMEOUT,
                             timeout_code="STOP_TIMEOUT")
                except HarnessError:
                    pass
                self._stopped = True
                try:
                    self.proc.wait(timeout=hs.STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=hs.STOP_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
        finally:
            self._close_streams()

    def _close_streams(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None
        if self.proc is not None:
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        self.reader = None
        self.writer = None

    def digest(self):
        return self._handshake["digest"] if self._handshake else None

    def tool_schemas(self):
        if not self._caps.get("tools.native"):
            return []
        return [{"name": "fs.read", "parameters": {"path": "string"}}]
