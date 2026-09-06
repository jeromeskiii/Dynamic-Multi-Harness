"""JSON-RPC 2.0 Protocol Server for DMH host.

Exposes DMH control plane capabilities over stdio or sockets for integration
with IDEs, agent orchestrators (such as Orca), and command-line runners.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from .capabilities import HOST_CAPABILITIES
from .errors import HarnessError
from .host import Host
from .telemetry import default_logger, redact_secrets


class DMHProtocolServer:
    """Standard JSON-RPC 2.0 stdio / socket server for DMH."""

    def __init__(self, host: Host, reader=None, writer=None, logger=None):
        self.host = host
        self.reader = reader or sys.stdin
        self.writer = writer or sys.stdout
        self.logger = logger or default_logger
        self._running = True
        self._lock = threading.Lock()
        self._methods = {
            "initialize": self._handle_initialize,
            "session/create": self._handle_session_create,
            "session/list": self._handle_session_list,
            "session/get": self._handle_session_get,
            "session/send": self._handle_session_send,
            "session/switch": self._handle_session_switch,
            "session/events": self._handle_session_events,
            "session/delete": self._handle_session_delete,
            "shutdown": self._handle_shutdown,
        }

    def run_forever(self) -> None:
        """Run the JSON-RPC dispatch loop over stdio."""
        self.logger.info("DMH Protocol Server started on stdio")
        while self._running:
            try:
                line = self.reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                self.dispatch_line(line)
            except (KeyboardInterrupt, SystemExit):
                break
            except Exception as exc:
                self.logger.error("Error reading JSON-RPC frame", {"error": str(exc)})
        self.host.stop_all()
        self.logger.info("DMH Protocol Server terminated")

    def dispatch_line(self, line: str) -> None:
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            self._send_error(None, -32700, f"Parse error: {exc}")
            return

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if not method or not isinstance(method, str):
            self._send_error(req_id, -32600, "Invalid Request: method missing")
            return

        handler = self._methods.get(method)
        if not handler:
            self._send_error(req_id, -32601, f"Method not found: {method}")
            return

        try:
            result = handler(params)
            if req_id is not None:
                self._send_result(req_id, result)
        except HarnessError as exc:
            self._send_error(req_id, -32000, exc.message, data={"code": exc.code, **exc.data})
        except Exception as exc:
            self.logger.error(f"Unhandled error in {method}", {"error": str(exc)})
            self._send_error(req_id, -32603, f"Internal error: {redact_secrets(str(exc))}")

    def _send_result(self, req_id: Any, result: Any) -> None:
        frame = {"jsonrpc": "2.0", "id": req_id, "result": result}
        self._write_frame(frame)

    def _send_error(self, req_id: Any, code: int, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        err: Dict[str, Any] = {"code": code, "message": redact_secrets(message)}
        if data:
            err["data"] = data
        frame = {"jsonrpc": "2.0", "id": req_id, "error": err}
        self._write_frame(frame)

    def send_notification(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        frame = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self._write_frame(frame)

    def _write_frame(self, frame: Dict[str, Any]) -> None:
        with self._lock:
            payload = json.dumps(frame, separators=(",", ":")) + "\n"
            try:
                self.writer.write(payload)
                self.writer.flush()
            except (OSError, ValueError):
                pass

    # ---- Handlers ----
    def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "abiVersion": 2,
            "serverInfo": {
                "name": "dmh-server",
                "version": "0.1.0",
                "storeDir": self.host.store_dir,
            },
            "capabilities": dict(HOST_CAPABILITIES),
            "supportedRuntimes": list(self.host.runtimes.keys()),
        }

    def _handle_session_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        preset = params.get("preset", "default")
        runtime_id = params.get("runtime", "native")
        session = self.host.create_session(preset=preset)
        session.bind(runtime_id)
        return {
            "sessionId": session.id,
            "preset": session.preset,
            "runtime": session.runtime_id,
            "epoch": session.epoch,
        }

    def _handle_session_list(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        sessions = self.host.list_sessions()
        return [s.to_dict() for s in sessions]

    def _handle_session_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        session_id = params.get("sessionId")
        if not session_id:
            raise HarnessError("INVALID_PARAMS", "sessionId is required")
        session = self.host.get_session(session_id) or self.host.resume_session(session_id)
        return {
            "sessionId": session.id,
            "preset": session.preset,
            "runtime": session.runtime_id,
            "epoch": session.epoch,
            "turnNo": session.turn_no,
            "messages": session.log.derive_messages(),
            "eventCount": len(session.log.events),
        }

    def _handle_session_send(self, params: Dict[str, Any]) -> Dict[str, Any]:
        session_id = params.get("sessionId")
        text = params.get("text", "")
        if not session_id:
            raise HarnessError("INVALID_PARAMS", "sessionId is required")

        session = self.host.get_session(session_id)
        if not session:
            session = self.host.resume_session(session_id)

        session.submit(text)
        last_text = session.log.last_assistant_text()
        return {
            "ok": True,
            "turn": session.turn_no,
            "epoch": session.epoch,
            "text": last_text,
        }

    def _handle_session_switch(self, params: Dict[str, Any]) -> Dict[str, Any]:
        session_id = params.get("sessionId")
        to = params.get("to")
        reason = params.get("reason", "api_switch")
        if not session_id or not to:
            raise HarnessError("INVALID_PARAMS", "sessionId and to are required")

        session = self.host.get_session(session_id)
        if not session:
            session = self.host.resume_session(session_id)

        session.switch_runtime(to, reason=reason)
        return {
            "ok": True,
            "currentRuntime": session.runtime_id,
            "epoch": session.epoch,
        }

    def _handle_session_events(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        session_id = params.get("sessionId")
        after_seq = int(params.get("after", 0))
        if not session_id:
            raise HarnessError("INVALID_PARAMS", "sessionId is required")

        session = self.host.get_session(session_id)
        if not session:
            session = self.host.resume_session(session_id)

        events = session.log.events
        return [e.to_dict() for e in events if e.seq > after_seq]

    def _handle_session_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        session_id = params.get("sessionId")
        if not session_id:
            raise HarnessError("INVALID_PARAMS", "sessionId is required")
        if session_id in self.host.sessions:
            self.host.sessions[session_id].shutdown()
            del self.host.sessions[session_id]
        deleted = self.host.store.delete_session(session_id)
        return {"ok": deleted}

    def _handle_shutdown(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._running = False
        self.host.stop_all()
        return {"ok": True}
