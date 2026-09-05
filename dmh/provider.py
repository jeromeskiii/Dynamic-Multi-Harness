"""The harness provider protocol.

The host owns the log, inbox, approval plane and sandbox world; providers own
prompt assembly internals, loop state machines and their private tool
registries. Providers propose; the host decides (design doc topology B).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import hashlib
import json


@dataclass
class StreamItem:
    kind: str  # "chunk" | "tool_call" | "step_end"
    payload: dict = field(default_factory=dict)


def compute_schema_hash(runtime_id, caps, tool_names):
    surface = {
        "runtime_id": runtime_id,
        "caps": sorted(k for k, v in caps.items() if v),
        "tools": sorted(tool_names),
    }
    digest = hashlib.sha256(
        json.dumps(surface, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


class HarnessProvider(ABC):
    """A replaceable harness driver. One spawn per bind: no reattach."""

    runtime_id = "abstract"

    # -- lifecycle --
    def launch(self):
        """Optional Layer-0 process spawn. Returns launch facts for the log."""
        return {"runtime_id": self.runtime_id, "transport": "in-process"}

    @abstractmethod
    def initialize(self, host_caps, constraints):
        """Layer 1: handshake the contract. Returns the runtime's initialize
        result (abiVersion, runtimeInfo, runtimeCapabilities, schemaHash)."""

    @abstractmethod
    def configure(self, snapshot):
        """Frozen config snapshot: model, cwd, preset, sandbox handle."""

    @abstractmethod
    def open_turn(self, epoch, projection):
        """Reconstruction epochs stay host-side: the projection is derived
        from the log, never from a runtime's leftover RAM."""

    @abstractmethod
    def run_step(self, resume=None):
        """Yield StreamItems; the final item must be kind='step_end' with
        payload {turn_end: bool, aborted: None|'before_dispatch'|'drained',
        error?: str}."""

    @abstractmethod
    def cancel(self):
        """Child abort != session abort. Drain to quiescence; the host stays
        alive and appends ABORTED."""

    @abstractmethod
    def stop(self):
        """Graceful Stop/Close; SIGKILL after the STOP budget."""

    # -- introspection --
    @abstractmethod
    def capabilities(self):
        """Omitted capability = unsupported (false)."""

    def tool_schemas(self):
        """Native tool surface. Rebuilt per-runtime after every switch; the
        schema hash is logged on each request/header."""
        return []

    def schema_hash(self):
        return compute_schema_hash(
            self.runtime_id, self.capabilities(),
            [t["name"] for t in self.tool_schemas()],
        )
