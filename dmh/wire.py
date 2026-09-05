"""NDJSON JSON-RPC 2.0 framing with select-based deadline reads.

stdout is handshake + protocol frames only. One JSON object per line.
Requests, results, errors and notifications all JSON-RPC 2.0.
Notifications have no id; _-prefixed methods are extensions.
"""

import json
import os
import select
import time

from .errors import HarnessError


def dumps(obj):
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


def loads(line):
    try:
        obj = json.loads(bytes(line).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessError("HANDSHAKE_MALFORMED", f"unparseable frame: {exc}") from None
    if not isinstance(obj, dict):
        raise HarnessError("HANDSHAKE_MALFORMED", "frame is not a JSON object")
    return obj


def request(req_id, method, params=None):
    frame = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        frame["params"] = params
    return frame


def notification(method, params=None):
    frame = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        frame["params"] = params
    return frame


def response(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def error_response(req_id, code, message, data=None):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32001,
            "message": message,
            "data": {"errorCode": code, **(data or {})},
        },
    }


def is_request(obj):
    return "method" in obj and "id" in obj


def is_notification(obj):
    return "method" in obj and "id" not in obj


def is_response(obj):
    return "id" in obj and "method" not in obj


class LineReader:
    """Deadline-aware line reader over a raw fd (pipe or socket)."""

    def __init__(self, fd):
        self.fd = fd
        self._buf = b""

    def readline(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                return line
            if deadline is None:
                wait = None
            else:
                wait = deadline - time.monotonic()
                if wait < 0:
                    wait = 0  # non-blocking poll attempt before giving up
            try:
                ready, _, _ = select.select([self.fd], [], [], wait)
            except InterruptedError:
                continue
            if not ready:
                return None
            chunk = os.read(self.fd, 65536)
            if not chunk:
                if self._buf:
                    line, self._buf = self._buf, b""
                    return line
                raise EOFError("stream closed")
            self._buf += chunk


class LineWriter:
    def __init__(self, fd):
        self.fd = fd

    def send(self, obj):
        payload = dumps(obj)
        while payload:
            payload = payload[os.write(self.fd, payload):]

    def send_raw(self, text):
        payload = (text + "\n").encode("utf-8")
        while payload:
            payload = payload[os.write(self.fd, payload):]
