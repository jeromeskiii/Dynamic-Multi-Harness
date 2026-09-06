"""Host-owned session log: append-only, replay-stable, fork-safe.

The session log is the join key (design doc). Surface events are the only
model-visible truth; handshake/approval/overflow facts are log-only and never
enter derive_messages(). Every transition hits disk (fsync) before the next
step commences.
"""

import json
import os
import time

from .errors import HarnessError

SURFACE_KINDS = frozenset({
    "runtime/bind",
    "runtime/switch",
    "runtime/unbind",
    "turn/start",
    "step/start",
    "step/end",
    "user/message",
    "assistant/chunk",
    "assistant/message",
    "tool/call",
    "tool/result",
    "turn/end",
})

CONTROL_KINDS = frozenset({
    "runtime/launch",
    "runtime/handshake",
    "runtime/initialize",
    "runtime/bind-failed",
    "approval/request",
    "approval/decided",
    "session/delegate",
    "turn/abort",
    "turn/abort-before-dispatch",
    "security/finding",
    "overflow/dropped",
    "overflow/paused",
})

KNOWN_KINDS = SURFACE_KINDS | CONTROL_KINDS


class Event:
    __slots__ = ("seq", "ts", "session_id", "kind", "payload", "runtime_id", "epoch")

    def __init__(self, seq, ts, session_id, kind, payload, runtime_id=None, epoch=0):
        self.seq = seq
        self.ts = ts
        self.session_id = session_id
        self.kind = kind
        self.payload = payload
        self.runtime_id = runtime_id
        self.epoch = epoch

    @property
    def is_surface(self):
        return self.kind in SURFACE_KINDS

    def to_dict(self):
        return {
            "seq": self.seq,
            "ts": self.ts,
            "session": self.session_id,
            "kind": self.kind,
            "payload": self.payload,
            "runtime": self.runtime_id,
            "epoch": self.epoch,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            d["seq"],
            d["ts"],
            d["session"],
            d["kind"],
            d.get("payload") or {},
            d.get("runtime"),
            d.get("epoch") or 0,
        )

    def __repr__(self):
        return f"Event({self.seq}, {self.kind}, runtime={self.runtime_id!r})"



class SessionLog:
    """Append-only JSONL log; one file per session."""

    def __init__(self, session_id, store_dir):
        self.session_id = session_id
        self.store_dir = store_dir
        self.path = os.path.join(store_dir, "sessions", f"{session_id}.jsonl")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._seq = 0
        self._events = []
        self._fh = open(self.path, "a", encoding="utf-8")

    def close(self):
        """Close the underlying JSONL file handle. Idempotent."""
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def __del__(self):
        self.close()

    def append(self, kind, payload=None, runtime_id=None, epoch=0):
        if kind not in KNOWN_KINDS:
            raise ValueError(f"unknown event kind: {kind!r}")
        self._seq += 1
        event = Event(
            self._seq, time.time(), self.session_id, kind,
            dict(payload or {}), runtime_id, epoch,
        )
        if self._fh is None:
            raise HarnessError("LOG_CLOSED", "cannot append to closed session log")
        self._fh.write(
            json.dumps(event.to_dict(), separators=(",", ":")) + "\n"
        )
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._events.append(event)
        return event

    @property
    def events(self):
        return list(self._events)

    def reload(self):
        """Replay from disk: append-only stream must project identically."""
        self._events = []
        self._seq = 0
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                event = Event.from_dict(json.loads(line))
                self._events.append(event)
                self._seq = event.seq
        return self._events

    def surface_events(self):
        return [e for e in self._events if e.is_surface]

    def derive_messages(self):
        """Model-visible truth built from surface events only. Private
        assistant chunk streams fold into assistant/message; handshake and
        approval facts never appear here."""
        messages = []
        for e in self.surface_events():
            if e.kind == "user/message":
                messages.append({"role": "user", "content": e.payload.get("text", "")})
            elif e.kind == "assistant/message":
                messages.append({"role": "assistant", "content": e.payload.get("text", "")})
            elif e.kind == "tool/result":
                messages.append({
                    "role": "tool",
                    "name": e.payload.get("tool", ""),
                    "content": e.payload.get("content", ""),
                })
        return messages

    def events_until_switch(self):
        """Surface projection up to (not including) the first runtime/switch
        marker - the part every runtime must agree on."""
        out = []
        for e in self._events:
            if e.kind == "runtime/switch":
                break
            if e.is_surface:
                out.append(e)
        return out

    def is_open_turn(self):
        depth = 0
        for e in self._events:
            if e.kind == "turn/start":
                depth += 1
            elif e.kind == "turn/end":
                depth -= 1
        return depth > 0

    def fork(self, new_session_id):
        """Fork must refuse an open-turn boundary; the parent is never
        mutated."""
        if self.is_open_turn():
            raise HarnessError("TURN_OPEN", "cannot fork a session with an open turn")
        child = SessionLog(new_session_id, self.store_dir)
        for e in self._events:
            child.append(e.kind, e.payload, e.runtime_id, e.epoch)
        return child

    def adopt(self, events):
        for e in events:
            self.append(e.kind, e.payload, e.runtime_id, e.epoch)

    def last_assistant_text(self):
        for e in reversed(self._events):
            if e.kind == "assistant/message":
                return e.payload.get("text", "")
        return ""

    def count_kind(self, kind):
        return sum(1 for e in self._events if e.kind == kind)
