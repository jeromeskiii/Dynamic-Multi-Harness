"""The host kernel: log, inbox, approval, sandbox world, runtime registry.

The host owns every fact a runtime can see. Runtimes propose; the host
decides. This module implements topology B from the design doc: a Terraform-
style core with OpenTofu-style replaceable runtime providers.
"""

import os
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import capabilities as caps_mod
from .capabilities import HOST_CAPABILITIES
from .errors import HarnessError
from .events import SessionLog
from .handshake import redact
from .persistence import SessionMetadata, SessionStore
from .presets import preset_requires
from .provider import HarnessProvider
from .telemetry import default_logger


class SandboxWorld:
    """Path-confined read surface shared with every runtime. The host owns
    ctx.fs; children reach it only through the reserved transport."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def fs_read(self, path, limit=4000):
        try:
            target = os.path.abspath(os.path.join(self.root, path))
        except ValueError:
            return False, f"invalid path: {path!r}"
        if not (target == self.root or target.startswith(self.root + os.sep)):
            return False, f"path escapes sandbox: {path!r}"
        if not os.path.isfile(target):
            return False, f"no such file: {path!r}"
        with open(target, encoding="utf-8", errors="replace") as fh:
            return True, fh.read(limit)

    def write(self, name, content):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path


class ApprovalPolicy:
    """Approval is a host seam. Children emit ask; they do not own the
    decision."""

    def decide(self, tool, args):
        raise NotImplementedError


class AllowAll(ApprovalPolicy):
    def decide(self, tool, args):
        return True


class DenyAll(ApprovalPolicy):
    def decide(self, tool, args):
        return False


class AllowReadOnly(ApprovalPolicy):
    """Default: read-only tools pass, anything else requires a human (denied
    in headless mode)."""

    def decide(self, tool, args):
        return tool == "fs.read"


def build_snapshot(preset, sandbox_root, effective_caps):
    """Frozen config snapshot. Native tool schemas are only sent when the
    effective capability intersection has tools.native - otherwise the host
    must not advertise them."""
    return {
        "model": "mock",
        "cwd": os.getcwd(),
        "preset": preset,
        "sandbox": sandbox_root,
        "tools": [{"name": "fs.read", "parameters": {"path": "string"}}]
        if effective_caps.get("tools.native") else [],
    }


@dataclass
class RuntimeSpec:
    runtime_id: str
    factory: object          # callable() -> HarnessProvider (fresh spawn per bind)
    declared_caps: dict = field(default_factory=dict)


MAX_DELEGATION_DEPTH = 2
MAX_STEPS_PER_TURN = 8



class Host:
    """The control plane. Owns the runtime registry, the store and every
    session. Runtimes are providers; the host is the only thing that writes
    the session log."""

    def __init__(self, store_dir=".dmh_state", approval_policy=None,
                 sandbox_root=None, logger=None):
        self.store_dir = os.path.abspath(store_dir)
        os.makedirs(self.store_dir, exist_ok=True)
        self.sandbox = SandboxWorld(sandbox_root or os.path.join(self.store_dir, "sandbox"))
        self.policy = approval_policy or AllowReadOnly()
        self.store = SessionStore(self.store_dir)
        self.logger = logger or default_logger
        self.runtimes = {}
        self.sessions = {}

    def register(self, runtime_id, factory, declared_caps=None):
        self.runtimes[runtime_id] = RuntimeSpec(runtime_id, factory, dict(declared_caps or {}))
        return self

    def create_session(self, preset="default", parent_id=None, delegation_depth=0):
        session = Session(
            host=self,
            session_id=uuid.uuid4().hex[:12],
            preset=preset,
            parent_id=parent_id,
            delegation_depth=delegation_depth,
        )
        self.sessions[session.id] = session
        session._sync_metadata()
        return session

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def list_sessions(self):
        return self.store.list_sessions()

    def resume_session(self, session_id, runtime_id=None):
        if session_id in self.sessions:
            session = self.sessions[session_id]
            if runtime_id and runtime_id != session.runtime_id:
                session.switch_runtime(runtime_id, reason="resume_rebind")
            return session

        meta = self.store.get_metadata(session_id)
        if not meta:
            meta = self.store.rebuild_metadata_from_log(session_id)

        session = Session(
            host=self,
            session_id=session_id,
            preset=meta.preset,
            parent_id=meta.parent_id,
            delegation_depth=meta.delegation_depth,
        )
        session.log.reload()
        session.turn_no = meta.turn_count
        session.epoch = meta.epoch

        bind_target = runtime_id or meta.current_runtime or "native"
        if bind_target in self.runtimes:
            session.bind(bind_target, reason="resume")

        # The fresh rebind appended its own events on top of the
        # metadata snapshot, so re-sync the manifest before handing the
        # session back.
        session._sync_metadata()
        self.sessions[session_id] = session
        return session

    def attach_signal_handlers(self):
        def _handler(signum, frame):
            self.logger.info(f"Received signal {signum}, shutting down sessions...")
            self.stop_all()
            sys.exit(0)

        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
        except (ValueError, AttributeError):
            pass

    def stop_all(self):
        for session in list(self.sessions.values()):
            session.shutdown()


class Session:
    """One session = one append-only log + one bound runtime + one epoch.

    Rebind is legal only at turn boundaries (never mid-stream); delegation
    opens child sessions with lineage and a depth guard.
    """

    def __init__(self, host, session_id, preset, parent_id=None, delegation_depth=0):
        self.host = host
        self.id = session_id
        self.preset = preset
        self.parent_id = parent_id
        self.delegation_depth = delegation_depth
        self.log = SessionLog(session_id, host.store_dir)
        self.epoch = 1
        self.turn_no = 0
        self.turn_open = False
        self.provider = None
        self.runtime_id = None
        self._last_runtime_id = None
        self.effective_caps = {}
        self._sink = None
        self._buffer = None
        self._pending_tool = None
        self._lock = threading.RLock()

    def _sync_metadata(self):
        try:
            created_at = self.log.events[0].ts if self.log.events else time.time()
            updated_at = self.log.events[-1].ts if self.log.events else time.time()
            meta = SessionMetadata(
                session_id=self.id,
                preset=self.preset,
                parent_id=self.parent_id,
                delegation_depth=self.delegation_depth,
                current_runtime=self.runtime_id or self._last_runtime_id,
                epoch=self.epoch,
                turn_count=self.turn_no,
                event_count=len(self.log.events),
                created_at=created_at,
                updated_at=updated_at,
            )
            self.host.store.record_session(meta)
        except Exception:
            pass

    # ---- bind / rebind ----
    def bind(self, runtime_id, reason="initial", extra_required=()):
        """Fail-loud bind: launch, handshake, initialize, capability gate."""
        with self._lock:
            events = self._bind(runtime_id, reason, extra_required)
            for kind, payload, rt, epoch in events:
                self.log.append(kind, payload, runtime_id=rt, epoch=epoch)
            failure = [e for e in events if e[0] == "runtime/bind-failed"]
            self._sync_metadata()
            if failure:
                raise HarnessError(failure[0][1]["errorCode"], failure[0][1]["message"])

    def _bind(self, runtime_id, reason, extra_required):
        spec = self.host.runtimes.get(runtime_id)
        if spec is None:
            raise HarnessError(
                "RUNTIME_FAULT", f"unknown runtime {runtime_id!r}"
            )
        provider = spec.factory()
        out = []
        try:
            facts = provider.launch() or {}
        except HarnessError as exc:
            return self._bind_failed_events(runtime_id, exc)
        out.append(("runtime/launch", {
            "runtime": runtime_id, "transport": facts.get("transport"),
            "instance": facts.get("instance"),
        }, None, self.epoch))
        if facts.get("net") or facts.get("abi") is not None:
            out.append(("runtime/handshake", {
                "runtime": runtime_id, "abi": facts.get("abi"),
                "net": facts.get("net"), "addr": facts.get("addr"),
                "wire": facts.get("wire"),
            }, None, self.epoch))
        try:
            init = provider.initialize(HOST_CAPABILITIES, {})
        except HarnessError as exc:
            provider.stop()
            return self._bind_failed_events(runtime_id, exc)
        eff = caps_mod.effective(HOST_CAPABILITIES, provider.capabilities())
        required = preset_requires(self.preset) + list(extra_required)
        missing = caps_mod.missing_capabilities(required, eff)
        if missing:
            provider.stop()
            out.extend(self._bind_failed_events(
                runtime_id,
                HarnessError(
                    "CAPABILITY_REQUIRED",
                    f"preset {self.preset!r} requires {missing}",
                    {"missing": missing},
                ),
            ))
            return out
        snapshot = build_snapshot(self.preset, self.host.sandbox.root, eff)
        try:
            provider.configure(snapshot)
        except HarnessError as exc:
            provider.stop()
            return self._bind_failed_events(runtime_id, exc)
        out.append(("runtime/initialize", {
            "runtime": runtime_id,
            "info": init.get("runtimeInfo") or {},
            "caps": dict(provider.capabilities()),
            "schema_hash": init.get("schemaHash") or provider.schema_hash(),
        }, None, self.epoch))
        out.append(("runtime/bind", {
            "runtime": runtime_id,
            "abi": init.get("abiVersion"),
            "caps": dict(eff),
            "schema_hash": init.get("schemaHash") or provider.schema_hash(),
            "reason": reason,
        }, runtime_id, self.epoch))
        self.provider = provider
        self.runtime_id = runtime_id
        self._last_runtime_id = runtime_id
        self.effective_caps = eff
        return out

    @staticmethod
    def _bind_failed_events(runtime_id, exc):
        out = [("runtime/bind-failed", {
            "runtime": runtime_id, "errorCode": exc.code,
            "message": exc.message, "data": exc.data,
        }, None, 0)]
        if exc.code == "BIND_NOT_LOOPBACK":
            out.append(("security/finding", {
                "code": exc.code, "runtime": runtime_id,
                "detail": redact(exc.message),
            }, None, 0))
        return out

    def switch_runtime(self, to, reason):
        """Per-turn rebind. Legal only at turn boundaries; requires
        replay.from_log on the incoming runtime. Never mid-stream."""
        with self._lock:
            if self.turn_open:
                raise HarnessError(
                    "TURN_OPEN", "runtime/switch is illegal with an open turn"
                )
            if self.runtime_id is None:
                raise HarnessError("RUNTIME_FAULT", "no runtime bound to switch from")
            old_provider = self.provider
            old_runtime = self.runtime_id
            events = self._bind(to, "switch", extra_required=("replay.from_log",))
            failure = [e for e in events if e[0] == "runtime/bind-failed"]
            if failure:
                for kind, payload, rt, epoch in events:
                    self.log.append(kind, payload, runtime_id=rt, epoch=epoch)
                raise HarnessError(
                    failure[0][1]["errorCode"], failure[0][1]["message"]
                )
            self.epoch += 1
            self.log.append("runtime/unbind", {"runtime": old_runtime, "reason": reason},
                            runtime_id=old_runtime, epoch=self.epoch)
            self.log.append("runtime/switch", {
                "from": old_runtime, "to": to, "reason": reason, "epoch": self.epoch,
            }, None, self.epoch)
            for kind, payload, rt, epoch in events:
                self.log.append(kind, payload, runtime_id=rt, epoch=self.epoch)
            self._sync_metadata()
            try:
                old_provider.stop()
            except HarnessError:
                pass


    # ---- turn lifecycle ----
    def submit(self, text):
        """Inbox injection: a user/message fact, then a governed turn."""
        with self._lock:
            if self.provider is None:
                raise HarnessError("RUNTIME_FAULT", "session is not bound to a runtime")
            self.log.append("user/message", {"text": text}, runtime_id=None, epoch=self.epoch)
            self.run_turn()

    def run_turn(self):
        with self._lock:
            if self.turn_open:
                raise HarnessError("TURN_OPEN", "turn already in progress")
            self.turn_open = True
            self.turn_no += 1
            self.log.append("turn/start", {
                "turn": self.turn_no, "epoch": self.epoch,
            }, None, self.epoch)
            projection = self.log.derive_messages()
            self.provider.open_turn(self.epoch, projection)
            aborted = None
            resume = None
            # Track how we leave the step loop so the right terminal fact lands
            # on the log. for-else: the else clause only fires when the loop
            # completes without break - i.e. MAX_STEPS_PER_TURN was hit and no
            # step ever set turn_end / abort / error.
            exhausted = False
            try:
                for step_no in range(1, MAX_STEPS_PER_TURN + 1):
                    step_id = f"{self.runtime_id}:{self.id}:{step_no}"
                    self.log.append("step/start", {
                        "step_id": step_id, "schema_hash": self.provider.schema_hash(),
                    }, self.runtime_id, self.epoch)
                    chunks = []
                    turn_end = False
                    error = None
                    for item in self.provider.run_step(resume=resume):
                        if item.kind == "chunk":
                            self.log.append("assistant/chunk", {"text": item.payload["text"]},
                                            runtime_id=self.runtime_id, epoch=self.epoch)
                            chunks.append(item.payload["text"])
                            self._push_sink("assistant/chunk", item.payload["text"])
                        elif item.kind == "tool_call":
                            resume = self._handle_tool_call(item.payload)
                            if chunks:
                                self.log.append("assistant/message", {"text": "".join(chunks)},
                                                runtime_id=self.runtime_id, epoch=self.epoch)
                                chunks = []
                        elif item.kind == "step_end":
                            turn_end = bool(item.payload.get("turn_end"))
                            aborted = item.payload.get("aborted")
                            error = item.payload.get("error")
                    if chunks:
                        self.log.append("assistant/message", {"text": "".join(chunks)},
                                        runtime_id=self.runtime_id, epoch=self.epoch)
                    self.log.append("step/end", {
                        "step_id": step_id,
                        "status": "aborted" if aborted else ("error" if error else "ok"),
                    }, self.runtime_id, self.epoch)
                    if aborted == "before_dispatch":
                        self.log.append("turn/abort-before-dispatch", {
                            "turn": self.turn_no,
                        }, self.runtime_id, self.epoch)
                        break
                    if aborted == "drained":
                        self.log.append("turn/abort", {
                            "turn": self.turn_no, "reason": "cancelled mid-stream, drained",
                        }, self.runtime_id, self.epoch)
                        break
                    if error:
                        self.log.append("turn/abort", {
                            "turn": self.turn_no, "reason": error,
                        }, self.runtime_id, self.epoch)
                        break
                    if turn_end:
                        break
                else:
                    exhausted = True
                if exhausted:
                    self.log.append("turn/abort", {
                        "turn": self.turn_no,
                        "reason": "max_steps_exceeded",
                    }, self.runtime_id, self.epoch)
                else:
                    self.log.append("turn/end", {
                        "turn": self.turn_no, "epoch": self.epoch,
                        "status": "aborted" if aborted else "ok",
                    }, self.runtime_id, self.epoch)
            finally:
                self.turn_open = False
                self._sync_metadata()



    def _handle_tool_call(self, payload):
        name = payload.get("name")
        args = payload.get("args") or {}
        approval_required = bool(payload.get("approval_required"))
        self.log.append("tool/call", {
            "tool": name, "args": args, "approval_required": approval_required,
            "schema_hash": self.provider.schema_hash(),
        }, self.runtime_id, self.epoch)
        approved = True
        if approval_required:
            self.log.append("approval/request", {"tool": name, "args": args},
                            self.runtime_id, self.epoch)
            approved = self.host.policy.decide(name, args)
            self.log.append("approval/decided", {"tool": name, "approved": approved},
                            None, self.epoch)
        if not approved:
            content = "denied by host approval policy"
            self.log.append("tool/result", {
                "tool": name, "status": "denied", "content": content,
            }, self.runtime_id, self.epoch)
            return {"status": "denied", "content": content}
        if name == "fs.read":
            ok, content = self.host.sandbox.fs_read(args.get("path", ""))
        else:
            ok, content = False, f"unknown tool: {name!r}"
        status = "ok" if ok else "denied"
        self.log.append("tool/result", {
            "tool": name, "status": status, "content": content,
        }, self.runtime_id, self.epoch)
        return {"status": status, "content": content}

    # ---- streaming sink (bounded buffer / flow control) ----
    def register_sink(self, sink, capacity):
        from .flow import BoundedEventBuffer
        self._sink = sink
        self._buffer = BoundedEventBuffer(capacity)

    def _push_sink(self, kind, text):
        if self._sink is None:
            return
        status, dropped = self._buffer.push((kind, text))
        if status == "dropped":
            self.log.append("overflow/dropped", {"count": dropped}, None, self.epoch)
        elif status == "paused":
            self.log.append("overflow/paused", {}, None, self.epoch)
        elif status == "ok":
            self._sink((kind, text))

    # ---- delegation ----
    def delegate_harness(self, runtime_id, objective):
        """One tool that opens a child session: lineage = parent,
        delegation_depth += 1, results come back as tool/result."""
        with self._lock:
            self.log.append("tool/call", {
                "tool": "delegate_harness",
                "args": {"runtime": runtime_id, "objective": objective},
                "schema_hash": self.provider.schema_hash() if self.provider else "",
            }, self.runtime_id, self.epoch)
            if self.delegation_depth >= MAX_DELEGATION_DEPTH:
                self.log.append("tool/result", {
                    "tool": "delegate_harness", "status": "refused",
                    "content": "DELEGATION_DEPTH_EXCEEDED",
                }, self.runtime_id, self.epoch)
                return None
            child = self.host.create_session(
                preset=self.preset,
                parent_id=self.id,
                delegation_depth=self.delegation_depth + 1,
            )
            child.bind(runtime_id)
            child.submit(objective)
            output = child.log.last_assistant_text()
            self.log.append("session/delegate", {
                "parent": self.id, "child": child.id,
                "depth": child.delegation_depth,
            }, self.runtime_id, self.epoch)
            self.log.append("tool/result", {
                "tool": "delegate_harness", "status": "ok",
                "child_session": child.id, "content": output,
            }, self.runtime_id, self.epoch)
            return child

    def unbind(self, reason="shutdown"):
        """Unbind the current runtime: stop the provider, append runtime/unbind,
        close the session log. Idempotent — safe to call on an already-unbound
        or already-shut-down session."""
        with self._lock:
            if self.provider is not None:
                try:
                    self.log.append("runtime/unbind", {
                        "runtime": self.runtime_id, "reason": reason,
                    }, runtime_id=self.runtime_id, epoch=self.epoch)
                except HarnessError:
                    pass  # log may already be closed; that is fine
                try:
                    self.provider.stop()
                except HarnessError:
                    pass
                self.provider = None
                self.runtime_id = None
            self.log.close()
            self._sync_metadata()

    # ---- cancellation / shutdown ----
    def cancel(self):
        """Child abort != session abort: drain the child, append ABORTED,
        keep the host idle."""
        if self.provider is not None:
            self.provider.cancel()

    def shutdown(self):
        self.unbind(reason="session/end")
