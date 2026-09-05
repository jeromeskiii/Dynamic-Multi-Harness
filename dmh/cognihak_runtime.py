"""Host-side adapter: spawn cognihak and translate onto its own protocol.

cognihak already speaks newline-delimited JSON-RPC over stdio, but with its
own method set (``session/*``, ``agent/*``, ``shutdown``) and its own async
semantics: ``agent/send`` returns ``{ok: true}`` immediately and the turn
unfolds in a session log you poll. So this provider does not speak the DMH
ABI to the child - it *translates*, driving one protocol from the other:

    DMH ABI                          cognihak protocol
    --------                         -----------------
    launch                           spawn ``cog --profile protocol``
    initialize                       session/list (liveness) + session/create
    open_turn(epoch, projection)     remember the projection
    step/run                         agent/send, then poll session/events
      step/chunk                       assistant/chunk delta
      step_end                         turn/end | step/end(status=error)
    cancel                           boundary flag only - no agent/cancel
    shutdown                         shutdown + close stdin

The translation is the honest part. The DMH ABI assumes the runtime can
rebuild itself from the host's log projection; cognihak's protocol has no
way to inject history (``agent/send`` takes only a message), so this runtime
declares ``replay.from_log: false``. The host enforces that claim:
``switch_runtime`` requires ``replay.from_log``, so a session can *bind*
cognihak but cannot *switch to* it mid-session. That is fail-closed by
design, not a defect to be configured away.

One session per bind, created fresh. Context across turns accumulates in
cognihak's own log, which is runtime state the host does not reconstruct -
the mirror image of the host never seeing cognihak's internals.
"""

import json
import os
import subprocess
import time
from pathlib import Path

from .errors import HarnessError
from .provider import HarnessProvider, StreamItem, compute_schema_hash

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])

COGNIHAK_CAPABILITIES = {
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

# Ambient environment the child never sees. The spawn gets a minimal allowlist
# plus the explicit COG_* seam - same discipline as hs.child_env, applied to a
# runtime that does not speak the handshake protocol.
BASE_ENV_KEYS = ("PATH", "HOME", "TMPDIR")


def resolve_project_root(project_root=None):
    if project_root is not None:
        return str(Path(project_root).resolve())
    env_root = os.environ.get("COGNIHAK_ROOT")
    if env_root:
        return str(Path(env_root).resolve())
    default = Path(__file__).resolve().parents[1] / ".." / "cognihak"
    return str(default.resolve())


def resolve_entry(root):
    return os.path.join(root, "apps", "cli", "src", "index.ts")


def resolve_tsx(root, tsx=None):
    if tsx is not None:
        return tsx
    env_tsx = os.environ.get("COGNIHAK_TSX")
    if env_tsx:
        return env_tsx
    local = os.path.join(root, "node_modules", ".bin", "tsx")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return "tsx"


def available(tsx=None, project_root=None):
    """Whether a bind can even be attempted. Callers gate on this rather than
    re-deriving the paths, so one resolution rule decides both."""
    root = resolve_project_root(project_root)
    entry = resolve_entry(root)
    interpreter = resolve_tsx(root, tsx)
    if os.path.isabs(interpreter):
        if not (os.path.isfile(interpreter) and os.access(interpreter, os.X_OK)):
            return False
    return os.path.isfile(entry)


class CognihakRuntime(HarnessProvider):
    """The cognihak harness as a provider, through its own stdio protocol."""

    runtime_id = "cognihak"

    def __init__(self, *, tsx=None, project_root=None, env=None,
                 api_base=None, api_key=None, persistence_root=None,
                 step_timeout=STEP_TIMEOUT, profile="protocol"):
        self._root = resolve_project_root(project_root)
        self._entry = resolve_entry(self._root)
        self._tsx = resolve_tsx(self._root, tsx)
        self._env = dict(env or {})
        self._api_base = api_base or os.environ.get("COG_API_BASE")
        self._api_key = api_key or os.environ.get("COG_API_KEY")
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
            env["COG_API_BASE"] = self._api_base
        if self._api_key:
            env["COG_API_KEY"] = self._api_key
        if self._persistence_root:
            env["COG_PERSISTENCE_ROOT"] = self._persistence_root
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
                f"cognihak entrypoint missing: {self._entry}",
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
            "transport": "stdio+cognihak-jsonrpc",
            "entry": self._entry,
        }

    def _rpc(self, method, params=None, timeout=10.0):
        if self.proc is None or self.proc.poll() is not None:
            raise HarnessError(
                "RUNTIME_FAULT", "cognihak process is gone"
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
            "RUNTIME_FAULT", "cognihak closed the stream", {"stderr_tail": tail}
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
        return dict(COGNIHAK_CAPABILITIES)

    def initialize(self, host_caps, constraints):
        # session/list doubles as the boot probe: the kernel has a 30s
        # per-plugin boot deadline, and this is how we wait it out.
        deadline = time.monotonic() + BOOT_TIMEOUT
        while True:
            try:
                self._rpc("session/list", {}, timeout=5.0)
                break
            except HarnessError as exc:
                if time.monotonic() >= deadline:
                    raise HarnessError(
                        "RUNTIME_FAULT",
                        f"cognihak did not become ready: {exc.message}",
                        exc.data,
                    ) from None
                time.sleep(0.1)
        result = self._rpc("session/create", {}, timeout=BOOT_TIMEOUT)
        self.session_id = (result or {}).get("sessionId")
        if not self.session_id:
            raise HarnessError(
                "RUNTIME_FAULT", "session/create returned no sessionId"
            )
        return {
            "abiVersion": 2,
            "runtimeInfo": {
                "name": "cognihak",
                "version": "0.1.0",
                "vendor": "cognihak",
                "protocol": "cognihak-jsonrpc-stdio",
                "digest": None,
            },
            "runtimeCapabilities": dict(COGNIHAK_CAPABILITIES),
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
            # No agent/cancel exists on the protocol carrier, so cancellation
            # is observable at step boundaries only.
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
            # cognihak may emit a complete message without chunked deltas
            # (e.g. compaction output, summarization). Surface it as a single
            # chunk so derive_messages() sees the text instead of an empty
            # assistant message.
            text = payload.get("text") or payload.get("content") or ""
            if text:
                return StreamItem("chunk", {"text": text})
            return None
        if etype == "step/end" and payload.get("status") == "error":
            raise HarnessError(
                "RUNTIME_FAULT",
                f"cognihak step failed: {payload.get('error', 'unknown')}",
            )
        # turn/*, step boundaries, tool events: the host derives its own
        # facts from the chunks; cognihak's internals stay its own.
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
                self.proc.stdin.close()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
        finally:
            for stream in (self.proc.stdout, self.proc.stderr):
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


def register(host, runtime_id="cognihak", **kwargs):
    """Register CognihakRuntime with a host. Convenience for host.register()."""
    host.register(
        runtime_id,
        lambda: CognihakRuntime(**kwargs),
        declared_caps=COGNIHAK_CAPABILITIES,
    )
