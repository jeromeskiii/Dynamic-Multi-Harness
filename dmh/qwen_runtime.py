"""Host-side adapter: spawn Qwen3.8-Flash-Next-Harness and translate onto its protocol.

Qwen3.8-Flash-Next-Harness speaks newline-delimited JSON-RPC over stdio, with
the H1 method set (``session/*``, ``agent/*``, ``shutdown``). This provider
translates DMH ABI provider calls into the Qwen harness JSON-RPC protocol:

    DMH ABI                          Qwen harness protocol
    --------                         ---------------------
    launch                           spawn ``dsh --profile protocol``
    initialize                       session/list (liveness + session ID discovery)
    open_turn(epoch, projection)     remember the projection
    step/run                         agent/send, then poll session/events
      step/chunk                       assistant/chunk delta
      step_end                         turn/end | step/end(status=error)
    cancel                           boundary flag only - no agent/cancel
    shutdown                         shutdown + close stdin

Like cognihak, Qwen3.8-Flash-Next-Harness does not accept history injection
on agent/send, so this runtime declares ``replay.from_log: false``.
Switching out is allowed; switching into mid-session fails closed.
"""

import json
import os
import subprocess
import time
from pathlib import Path

from .errors import HarnessError
from .provider import HarnessProvider, StreamItem, compute_schema_hash

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])

QWEN_CAPABILITIES = {
    "streaming": True,
    "replay.from_log": False,
    "tools.native": False,
    "tools.code": False,
    "compaction": False,
    "approval.ask": False,
    "subagents.native": False,
    "plan": False,
    "goals": False,
    "mcp.client": False,
    "prompt.image": False,
    "fs.world": False,
    "sandbox.world": False,
    "runtime.multiSession": False,
}

BOOT_TIMEOUT = 35.0
STEP_TIMEOUT = 60.0
POLL_INTERVAL = 0.05

BASE_ENV_KEYS = ("PATH", "HOME", "TMPDIR")


def resolve_project_root(project_root=None):
    if project_root is not None:
        return str(Path(project_root).resolve())
    env_root = os.environ.get("QWEN_ROOT")
    if env_root:
        return str(Path(env_root).resolve())
    default = Path(__file__).resolve().parents[1] / ".." / "Qwen3.8-Flash-Next-Harness"
    return str(default.resolve())


def resolve_entry(root):
    return os.path.join(root, "apps", "cli", "src", "index.ts")


def resolve_tsx(root, tsx=None):
    if tsx is not None:
        return tsx
    env_tsx = os.environ.get("QWEN_TSX")
    if env_tsx:
        return env_tsx
    local = os.path.join(root, "node_modules", ".bin", "tsx")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return "tsx"


def available(tsx=None, project_root=None):
    """Whether a bind can even be attempted."""
    root = resolve_project_root(project_root)
    entry = resolve_entry(root)
    interpreter = resolve_tsx(root, tsx)
    if os.path.isabs(interpreter):
        if not (os.path.isfile(interpreter) and os.access(interpreter, os.X_OK)):
            return False
    return os.path.isfile(entry)


class QwenRuntime(HarnessProvider):
    """The Qwen3.8-Flash-Next harness as a provider, through its stdio protocol."""

    runtime_id = "qwen3.8-flash-next"

    def __init__(self, *, tsx=None, project_root=None, env=None,
                 api_base=None, api_key=None, persistence_root=None,
                 step_timeout=STEP_TIMEOUT, profile="protocol"):
        self._root = resolve_project_root(project_root)
        self._entry = resolve_entry(self._root)
        self._tsx = resolve_tsx(self._root, tsx)
        self._env = dict(env or {})
        self._api_base = api_base or os.environ.get("QSH_API_BASE")
        self._api_key = api_key or os.environ.get("QSH_API_KEY")
        self._persistence_root = persistence_root
        self._profile = profile
        self._step_timeout = step_timeout
        self.proc = None
        self.session_id = None
        self._projection = []
        self._snapshot = {}
        self._cursor = -1
        self._rpc_id = 0
        self._cancel = False
        self._stopped = False

    # ---- spawn ----

    def _child_env(self):
        env = {"TMPDIR": os.environ.get("TMPDIR") or "/tmp"}
        for key in BASE_ENV_KEYS:
            if key in os.environ:
                env[key] = os.environ[key]
        if self._api_base:
            env["QSH_API_BASE"] = self._api_base
        if self._api_key:
            env["QSH_API_KEY"] = self._api_key
        if self._persistence_root:
            env["DSH_PERSISTENCE_ROOT"] = self._persistence_root
        for key, value in self._env.items():
            if "TOKEN" in key or "SECRET" in key or "KEY" in key:
                raise HarnessError(
                    "RUNTIME_FAULT", f"refusing to pass env key {key!r}"
                )
            env[key] = value
        return env

    def launch(self):
        if self.proc is not None:
            raise HarnessError(
                "RUNTIME_FAULT", "runtime already launched (one spawn per bind)"
            )
        if not os.path.isfile(self._entry):
            raise HarnessError(
                "RUNTIME_FAULT",
                f"Qwen entrypoint missing: {self._entry}",
                {"root": self._root},
            )
        argv = [self._tsx, self._entry, "--profile", self._profile]
        self.proc = subprocess.Popen(
            argv, cwd=self._root, env=self._child_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        return {
            "runtime_id": self.runtime_id,
            "transport": "stdio+qwen-jsonrpc",
            "entry": self._entry,
        }

    def _rpc(self, method, params=None, timeout=10.0):
        if self.proc is None or self.proc.poll() is not None:
            raise HarnessError(
                "RUNTIME_FAULT", "Qwen harness process is gone"
            )
        self._rpc_id += 1
        req_id = self._rpc_id
        frame = {"jsonrpc": "2.0", "id": req_id, "method": method,
                 "params": params or {}}
        try:
            self.proc.stdin.write(json.dumps(frame) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            self._raise_gone()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessError(
                    "RUNTIME_FAULT", f"timed out waiting for {method}"
                )
            line = self.proc.stdout.readline()
            if not line:
                self._raise_gone()
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                raise HarnessError(
                    "RUNTIME_FAULT", f"non-frame stdout: {line[:120]!r}"
                ) from None
            if reply.get("id") != req_id:
                continue
            if "error" in reply:
                err = reply["error"]
                raise HarnessError(
                    "RUNTIME_FAULT",
                    str(err.get("message", "protocol error")),
                    {"code": err.get("code"), "method": method},
                )
            return reply.get("result")

    def _raise_gone(self):
        tail = self._stderr_tail()
        raise HarnessError(
            "RUNTIME_FAULT", "Qwen harness closed the stream", {"stderr_tail": tail}
        )

    def _stderr_tail(self):
        if self.proc is None or self.proc.stderr is None:
            return ""
        try:
            return (self.proc.stderr.read() or "")[:500]
        except (OSError, ValueError):
            return ""

    # ---- provider contract ----

    def capabilities(self):
        return dict(QWEN_CAPABILITIES)

    def initialize(self, host_caps, constraints):
        deadline = time.monotonic() + BOOT_TIMEOUT
        while True:
            try:
                sessions = self._rpc("session/list", {}, timeout=5.0)
                if sessions and isinstance(sessions, list):
                    self.session_id = sessions[0]
                else:
                    self.session_id = f"qwen-{time.time_ns()}"
                break
            except HarnessError as exc:
                if time.monotonic() >= deadline:
                    raise HarnessError(
                        "RUNTIME_FAULT",
                        f"Qwen harness did not become ready: {exc.message}",
                        exc.data,
                    ) from None
                time.sleep(0.1)

        return {
            "abiVersion": 2,
            "runtimeInfo": {
                "name": "qwen3.8-flash-next",
                "version": "0.1.0",
                "vendor": "qwen",
                "protocol": "qwen-jsonrpc-stdio",
                "digest": None,
            },
            "runtimeCapabilities": dict(QWEN_CAPABILITIES),
            "authMethods": [],
            "schemaHash": self.schema_hash(),
        }

    def configure(self, snapshot):
        self._snapshot = dict(snapshot or {})

    def open_turn(self, epoch, projection):
        self._projection = list(projection or [])
        self._cancel = False

    def run_step(self, resume=None):
        if self._cancel:
            self._cancel = False
            yield StreamItem("step_end", {"turn_end": False, "aborted": "before_dispatch"})
            return
        if self.session_id is None:
            raise HarnessError("RUNTIME_FAULT", "runtime is not initialized")
        text = self._last_user()
        reply = self._rpc("agent/send", {"sessionId": self.session_id, "text": text})
        if not (reply or {}).get("ok", False):
            raise HarnessError(
                "RUNTIME_FAULT",
                f"agent/send refused: {(reply or {}).get('error', 'unknown')}",
            )
        yield from self._stream_turn()
        yield StreamItem("step_end", {"turn_end": True, "aborted": None})

    def _stream_turn(self):
        deadline = time.monotonic() + self._step_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessError("RUNTIME_FAULT", "turn stream timed out")
            events = self._rpc(
                "session/events",
                {"sessionId": self.session_id, "after": self._cursor},
                timeout=5.0,
            )
            for event in events or []:
                self._cursor = max(self._cursor, int(event.get("seq", 0)))
                item = self._translate(event)
                if item is not None:
                    yield item
                if event.get("type") == "turn/end":
                    return
            if self.proc.poll() is not None:
                self._raise_gone()
            time.sleep(POLL_INTERVAL)

    def _translate(self, event):
        etype = event.get("type")
        payload = event.get("payload") or {}
        if etype == "assistant/chunk":
            delta = payload.get("delta", "")
            if payload.get("kind", "content") == "content" and delta:
                return StreamItem("chunk", {"text": delta})
            return None
        if etype == "assistant/message":
            text = payload.get("text") or payload.get("content") or ""
            if text:
                return StreamItem("chunk", {"text": text})
            return None
        if etype == "step/end" and payload.get("status") == "error":
            raise HarnessError(
                "RUNTIME_FAULT",
                f"Qwen harness step failed: {payload.get('error', 'unknown')}",
            )
        return None

    def cancel(self):
        self._cancel = True

    def stop(self):
        if self.proc is None or self._stopped:
            return
        self._stopped = True
        try:
            if self.proc.poll() is None:
                try:
                    self._rpc("shutdown", {}, timeout=5.0)
                except HarnessError:
                    pass
                if self.proc.stdin is not None:
                    try:
                        self.proc.stdin.close()
                    except (OSError, ValueError):
                        pass
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
        finally:
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass

    def tool_schemas(self):
        return []

    def schema_hash(self):
        return compute_schema_hash(self.runtime_id, self.capabilities(), [])

    def _last_user(self):
        for message in reversed(self._projection):
            if message.get("role") == "user":
                return str(message.get("content", ""))
        return ""


def register(host, runtime_id="qwen3.8-flash-next", **kwargs):
    """Register QwenRuntime with a host. Convenience for host.register()."""
    host.register(
        runtime_id,
        lambda: QwenRuntime(**kwargs),
        declared_caps=QWEN_CAPABILITIES,
    )
