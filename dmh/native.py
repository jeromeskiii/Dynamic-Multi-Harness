"""The native loop driver: default in-process provider (topology B, mode A).

Same provider contract as the wire child, executed in-process against the
host sandbox world. Scripted and deterministic - the fixture runtime used by
tests and the day-one demo.
"""

import time

from .provider import HarnessProvider, StreamItem, compute_schema_hash
from .handshake import sha256_file

NATIVE_CAPABILITIES = {
    "streaming": True,
    "tools.native": True,
    "plan": True,
    "goals": False,
    "mcp.client": False,
    "approval.ask": True,
    "compaction": True,
    "replay.from_log": True,
    "subagents.native": True,
    "sandbox.world": True,
}

DEFAULT_SCRIPT = [{"say": "native-echo: {user}"}, {"end": True}]


def split_chunks(text, n=4):
    """Stream a message as up to n chunks covering the whole text, so
    streaming surfaces are exercised without dropping content."""
    if n <= 1 or len(text) <= n:
        return [text]
    step = -(-len(text) // n)  # ceil: at most n chunks, nothing dropped
    return [text[i:i + step] for i in range(0, len(text), step)]


class ScriptedNative(HarnessProvider):
    runtime_id = "native"

    def __init__(self, script=None, sandbox=None, caps=None, chunk_delay=0.0):
        self.script = list(script) if script is not None else None
        self.sandbox = sandbox
        self.caps = dict(caps or NATIVE_CAPABILITIES)
        self.chunk_delay = chunk_delay
        self._idx = 0
        self._turn_step = 0
        self._cancel = False
        self._projection = []
        self._snapshot = {}

    # -- provider contract --
    def capabilities(self):
        return dict(self.caps)

    def initialize(self, host_caps, constraints):
        return {
            "abiVersion": 2,
            "runtimeInfo": {
                "name": "native-loop",
                "version": "0.1.0",
                "vendor": "local",
                "digest": self.digest(),
            },
            "runtimeCapabilities": dict(self.caps),
            "authMethods": [],
            "schemaHash": self.schema_hash(),
        }

    def configure(self, snapshot):
        self._snapshot = dict(snapshot or {})

    def open_turn(self, epoch, projection):
        self._projection = list(projection or [])
        self._turn_step = 0

    def run_step(self, resume=None):
        if self._cancel:
            self._cancel = False
            yield StreamItem("step_end", {"turn_end": False, "aborted": "before_dispatch"})
            return
        self._turn_step += 1
        action = self._next_action()
        if action is None:
            yield StreamItem("step_end", {"turn_end": True})
            return
        if "fail" in action:
            yield StreamItem("step_end", {"turn_end": False, "error": str(action["fail"])})
            return
        for item in self._act(action, resume):
            if item.kind == "chunk" and self._cancel:
                self._cancel = False
                yield StreamItem("step_end", {"turn_end": False, "aborted": "drained"})
                return
            yield item
        yield StreamItem("step_end", {"turn_end": bool(action.get("end"))})

    def cancel(self):
        self._cancel = True

    def stop(self):
        pass

    def digest(self):
        return sha256_file(__file__)

    def tool_schemas(self):
        if not self.caps.get("tools.native"):
            return []
        return [{"name": "fs.read", "parameters": {"path": "string"}}]

    # -- scripted behavior --
    def _next_action(self):
        if self.script is not None:
            if self._idx >= len(self.script):
                return None
            action = self.script[self._idx]
            self._idx += 1
            return action
        # default: echo once, then end the turn
        if self._turn_step == 1:
            return {"say": "native-echo: {user}"}
        return {"end": True}

    def _act(self, action, resume):
        text = None
        if "say" in action:
            text = self._fmt(action["say"], resume)
        elif "slow_say" in action:
            text = self._fmt(action["slow_say"], resume)
        elif "say_result" in action:
            text = self._fmt(action["say_result"], resume)
        elif "tool" in action:
            tool = action["tool"]
            yield StreamItem("tool_call", {
                "name": tool.get("name"),
                "args": dict(tool.get("args") or {}),
                "approval_required": bool(tool.get("approval_required")),
            })
            return
        elif "host_read" in action:
            ok, content = self._host_read(action["host_read"].get("path", ""))
            text = content if ok else f"denied: {content}"
        elif "end" in action:
            return
        else:
            raise ValueError(f"unknown native action: {action!r}")
        for chunk in split_chunks(text):
            if self.chunk_delay:
                time.sleep(self.chunk_delay)
            yield StreamItem("chunk", {"text": chunk})

    def _host_read(self, path):
        if self.sandbox is None:
            return False, "no sandbox world"
        return self.sandbox.fs_read(path)

    def _fmt(self, text, resume):
        return (
            text.replace("{user}", self._last_user())
            .replace("{context}", str(len(self._projection)))
            .replace("{result}", str((resume or {}).get("content", "")))
        )

    def _last_user(self):
        for m in reversed(self._projection):
            if m.get("role") == "user":
                return m.get("content", "")
        return ""
