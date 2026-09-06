"""Session persistence, manifest indexing, and log integrity verification.

Manages session files, manifest catalog, atomic writes, and replay validation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from .errors import HarnessError


@dataclass
class SessionMetadata:
    session_id: str
    preset: str
    parent_id: Optional[str] = None
    delegation_depth: int = 0
    current_runtime: Optional[str] = None
    epoch: int = 1
    turn_count: int = 0
    event_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> SessionMetadata:
        return cls(
            session_id=d["session_id"],
            preset=d.get("preset", "default"),
            parent_id=d.get("parent_id"),
            delegation_depth=d.get("delegation_depth", 0),
            current_runtime=d.get("current_runtime"),
            epoch=d.get("epoch", 1),
            turn_count=d.get("turn_count", 0),
            event_count=d.get("event_count", 0),
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
        )


class SessionStore:
    """Authoritative disk store for sessions and manifest index."""

    def __init__(self, store_dir: str):
        self.store_dir = os.path.abspath(store_dir)
        self.sessions_dir = os.path.join(self.store_dir, "sessions")
        self.manifest_path = os.path.join(self.store_dir, "manifest.json")
        os.makedirs(self.sessions_dir, exist_ok=True)
        self._ensure_manifest()

    def _ensure_manifest(self) -> None:
        if not os.path.exists(self.manifest_path):
            self._write_manifest({})

    def _read_manifest(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_manifest(self, data: Dict[str, Dict[str, Any]]) -> None:
        temp_fd, temp_path = tempfile.mkstemp(dir=self.store_dir, prefix="manifest-tmp-")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_path, self.manifest_path)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def get_session_path(self, session_id: str) -> str:
        return os.path.join(self.sessions_dir, f"{session_id}.jsonl")

    def record_session(self, metadata: SessionMetadata) -> None:
        manifest = self._read_manifest()
        manifest[metadata.session_id] = metadata.to_dict()
        self._write_manifest(manifest)

    def get_metadata(self, session_id: str) -> Optional[SessionMetadata]:
        manifest = self._read_manifest()
        data = manifest.get(session_id)
        if data:
            return SessionMetadata.from_dict(data)
        # Fall back to rebuilding from disk if JSONL exists but manifest is missing
        path = self.get_session_path(session_id)
        if os.path.isfile(path):
            return self.rebuild_metadata_from_log(session_id)
        return None

    def list_sessions(self) -> List[SessionMetadata]:
        manifest = self._read_manifest()
        out = []
        for sid, data in manifest.items():
            if os.path.isfile(self.get_session_path(sid)):
                out.append(SessionMetadata.from_dict(data))
        # Sort by updated_at descending
        out.sort(key=lambda s: s.updated_at, reverse=True)
        return out

    def rebuild_metadata_from_log(self, session_id: str) -> SessionMetadata:
        path = self.get_session_path(session_id)
        if not os.path.isfile(path):
            raise HarnessError("SESSION_NOT_FOUND", f"session file {path} not found")

        events = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))

        created_at = events[0].get("ts", time.time()) if events else time.time()
        updated_at = events[-1].get("ts", created_at) if events else created_at
        turns = sum(1 for e in events if e.get("kind") == "turn/start")
        last_runtime = None
        epoch = 1
        for e in events:
            if e.get("runtime"):
                last_runtime = e.get("runtime")
            if e.get("epoch"):
                epoch = max(epoch, e.get("epoch"))

        meta = SessionMetadata(
            session_id=session_id,
            preset="default",
            current_runtime=last_runtime,
            epoch=epoch,
            turn_count=turns,
            event_count=len(events),
            created_at=created_at,
            updated_at=updated_at,
        )
        self.record_session(meta)
        return meta

    def verify_integrity(self, session_id: str) -> Tuple[bool, str]:
        """Verify sequential monotonicity and valid JSON formatting of a session log."""
        path = self.get_session_path(session_id)
        if not os.path.isfile(path):
            return False, f"file not found: {path}"

        expected_seq = 1
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line_idx, line in enumerate(fh, start=1):
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        return False, f"corrupted JSON at line {line_idx}: {exc}"

                    seq = record.get("seq")
                    if seq != expected_seq:
                        return False, f"seq mismatch at line {line_idx}: expected {expected_seq}, got {seq}"
                    expected_seq += 1

            return True, f"ok ({expected_seq - 1} events verified)"
        except OSError as exc:
            return False, f"read error: {exc}"

    def delete_session(self, session_id: str) -> bool:
        path = self.get_session_path(session_id)
        deleted = False
        if os.path.isfile(path):
            try:
                os.unlink(path)
                deleted = True
            except OSError:
                pass

        manifest = self._read_manifest()
        if session_id in manifest:
            del manifest[session_id]
            self._write_manifest(manifest)
            deleted = True
        return deleted
