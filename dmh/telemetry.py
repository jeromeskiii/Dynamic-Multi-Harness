"""Structured telemetry, spans, metrics, and secret redaction for DMH.

Provides NDJSON telemetry logging, span timing, error capture, and secret
redaction across all host surfaces and subprocess interfaces.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

# Secret redaction patterns
_SENSITIVE_PATTERNS = [
    # API keys and bearer tokens
    (re.compile(r"(?i)(bearer\s+)[a-zA-Z0-9_\-\.]{8,}"), r"\1[redacted-token]"),
    (re.compile(r"(?i)(api[_\-]?key\s*[:=]\s*['\"]?)[a-zA-Z0-9_\-\.]{8,}"), r"\1[redacted-key]"),
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "[redacted-openai-key]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{36}"), "[redacted-github-token]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[redacted-aws-key]"),
    # Passwords and secrets
    (re.compile(r"(?i)(password\s*[:=]\s*['\"]?)[^\s'\"]+"), r"\1[redacted-password]"),
    (re.compile(r"(?i)(secret\s*[:=]\s*['\"]?)[^\s'\"]+"), r"\1[redacted-secret]"),
]

# Magic cookie from Layer 0
MAGIC_COOKIE = "7f3c2a91e0b64d18c5a9f2d47b81e6c03d5a17b94e2c8f60a1d3b7c9e4f20815"


def redact_secrets(text: Any, limit: Optional[int] = None) -> str:
    """Scrub ambient secrets, bearer tokens, API keys, and magic cookies from text."""
    if text is None:
        return ""
    out = str(text)
    if limit is not None and len(out) > limit:
        out = out[:limit] + "... [truncated]"

    out = out.replace(MAGIC_COOKIE, "[redacted-cookie]")
    for pattern, repl in _SENSITIVE_PATTERNS:
        out = pattern.sub(repl, out)
    return out


class MetricCounter:
    """Simple thread-safe metric counter."""

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._value = 0

    def inc(self, amount: int = 1) -> int:
        self._value += amount
        return self._value

    @property
    def value(self) -> int:
        return self._value

    def reset(self) -> None:
        self._value = 0


class Span:
    """Execution span measuring wall-clock duration and tracking attributes."""

    def __init__(self, name: str, logger: Optional[TelemetryLogger] = None, attributes: Optional[Dict[str, Any]] = None):
        self.name = name
        self.logger = logger
        self.attributes = dict(attributes or {})
        self.start_time: float = 0.0
        self.end_time: float = 0.0
        self.duration_ms: float = 0.0
        self.error: Optional[str] = None

    def __enter__(self) -> Span:
        self.start_time = time.monotonic()
        if self.logger:
            self.logger.debug(f"span/start: {self.name}", {"span": self.name, **self.attributes})
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.end_time = time.monotonic()
        self.duration_ms = (self.end_time - self.start_time) * 1000.0
        if exc_val is not None:
            self.error = str(exc_val)
            if self.logger:
                self.logger.error(
                    f"span/error: {self.name} ({self.duration_ms:.2f}ms)",
                    {"span": self.name, "duration_ms": self.duration_ms, "error": redact_secrets(self.error), **self.attributes},
                )
        else:
            if self.logger:
                self.logger.debug(
                    f"span/end: {self.name} ({self.duration_ms:.2f}ms)",
                    {"span": self.name, "duration_ms": self.duration_ms, **self.attributes},
                )


class TelemetryLogger:
    """Structured NDJSON / human-readable logger with redaction and event hooks."""

    def __init__(
        self,
        name: str = "dmh",
        stream: Optional[Any] = None,
        json_format: Optional[bool] = None,
        min_level: str = "INFO",
    ):
        self.name = name
        self.stream = stream or sys.stderr
        self.json_format = (
            json_format
            if json_format is not None
            else (os.environ.get("DMH_LOG_FORMAT", "").lower() == "json")
        )
        self.min_level = os.environ.get("DMH_LOG_LEVEL", min_level).upper()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._levels = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}

    def add_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self._listeners.append(callback)

    def span(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> Span:
        return Span(name, logger=self, attributes=attributes)

    def _should_log(self, level: str) -> bool:
        return self._levels.get(level.upper(), 20) >= self._levels.get(self.min_level, 20)

    def log(self, level: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        if not self._should_log(level):
            return

        clean_message = redact_secrets(message)
        clean_payload = (
            {k: redact_secrets(v) if isinstance(v, str) else v for k, v in payload.items()}
            if payload
            else {}
        )

        record: Dict[str, Any] = {
            "ts": time.time(),
            "logger": self.name,
            "level": level.upper(),
            "msg": clean_message,
        }
        if clean_payload:
            record["payload"] = clean_payload

        for listener in self._listeners:
            try:
                listener(record)
            except Exception:
                pass

        if self.json_format:
            line = json.dumps(record, separators=(",", ":")) + "\n"
        else:
            p_str = f" {clean_payload}" if clean_payload else ""
            line = f"[{time.strftime('%H:%M:%S')}] [{level.upper()}] {clean_message}{p_str}\n"

        try:
            self.stream.write(line)
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def debug(self, msg: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.log("DEBUG", msg, payload)

    def info(self, msg: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.log("INFO", msg, payload)

    def warn(self, msg: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.log("WARN", msg, payload)

    def error(self, msg: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.log("ERROR", msg, payload)


# Global default logger instance
default_logger = TelemetryLogger("dmh.core")
