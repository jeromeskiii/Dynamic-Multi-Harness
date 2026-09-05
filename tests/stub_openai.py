"""A stdlib OpenAI-compatible stub endpoint (test fixture, not runtime code).

cognihak's only model adapter speaks OpenAI's streaming chat-completions
protocol, so driving a real turn requires an endpoint. This one is
deterministic and offline: no network, no keys, no vendor SDK. It lets the
cognihak provider tests exercise the genuine adapter path - the same fetch,
SSE framing, and delta handling a real endpoint would hit - without a model.

Not a DMH dependency: lives under tests/ and is imported only by tests.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DONE = "data: [DONE]\n\n"


def _chunk(text, finish=None):
    delta = {"role": "assistant", "content": text} if text else {}
    payload = {
        "id": "stub",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "stub-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def reply_for(messages, prefix="stub-echo"):
    """Deterministic reply: echo the newest user message."""
    last = ""
    for message in messages or []:
        if message.get("role") == "user":
            last = str(message.get("content") or "")
    return f"{prefix}: {last}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    prefix = "stub-echo"

    def _read_body(self):
        length = int(self.headers.get("content-length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _send_sse(self, body):
        text = reply_for(body.get("messages"), self.prefix)
        pieces = [text[i:i + 12] for i in range(0, len(text), 12)] or [text]
        out = "".join(_chunk(piece) for piece in pieces)
        out += _chunk("", finish="stop")
        out += DONE
        encoded = out.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(encoded)))
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, body):
        text = reply_for(body.get("messages"), self.prefix)
        payload = {
            "id": "stub",
            "object": "chat.completion",
            "created": 0,
            "model": "stub-model",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text},
                 "finish_reason": "stop"}
            ],
        }
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        body = self._read_body()
        if self.path.rstrip("/").endswith("/chat/completions"):
            if body.get("stream") is False:
                self._send_json(body)
            else:
                self._send_sse(body)
            return
        self._send_json(body)

    def do_GET(self):  # noqa: N802
        payload = {"object": "list", "data": [{"id": "stub-model"}]}
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        """Silence: request logging is noise in test output."""


class StubOpenAI:
    """Context manager: serves on loopback, ephemeral port."""

    def __init__(self, prefix="stub-echo"):
        self.prefix = prefix
        handler = type("Handler", (_Handler,), {"prefix": prefix})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    @property
    def requests(self):
        return 0

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
