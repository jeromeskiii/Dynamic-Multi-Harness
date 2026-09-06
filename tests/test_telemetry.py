"""Tests for telemetry, spans, metrics, and secret redaction."""

import io
import json
import unittest

from dmh.telemetry import (
    MAGIC_COOKIE,
    MetricCounter,
    Span,
    TelemetryLogger,
    redact_secrets,
)


class TelemetryTests(unittest.TestCase):
    def test_secret_redaction(self):
        text = f"Cookie: {MAGIC_COOKIE}, key: sk-abcdef1234567890abcdef1234, token: Bearer mySecretToken123"
        redacted = redact_secrets(text)
        self.assertNotIn(MAGIC_COOKIE, redacted)
        self.assertIn("[redacted-cookie]", redacted)
        self.assertNotIn("sk-abcdef1234567890abcdef1234", redacted)
        self.assertIn("[redacted-openai-key]", redacted)
        self.assertNotIn("mySecretToken123", redacted)
        self.assertIn("[redacted-token]", redacted)

    def test_metric_counter(self):
        counter = MetricCounter("turns_total", "Total turns run")
        self.assertEqual(counter.value, 0)
        counter.inc(1)
        counter.inc(2)
        self.assertEqual(counter.value, 3)
        counter.reset()
        self.assertEqual(counter.value, 0)

    def test_span_measurement(self):
        buf = io.StringIO()
        logger = TelemetryLogger(name="test", stream=buf, json_format=True, min_level="DEBUG")
        with Span("test_op", logger=logger, attributes={"session": "s1"}) as span:
            self.assertEqual(span.name, "test_op")
        self.assertGreaterEqual(span.duration_ms, 0.0)

        lines = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["msg"], "span/start: test_op")
        self.assertIn("span/end: test_op", lines[1]["msg"])
        self.assertEqual(lines[1]["payload"]["session"], "s1")

    def test_span_error_capture(self):
        buf = io.StringIO()
        logger = TelemetryLogger(name="test", stream=buf, json_format=True, min_level="DEBUG")
        with self.assertRaises(ValueError):
            with Span("failing_op", logger=logger):
                raise ValueError(f"Secret failure sk-123456789012345678901234")

        lines = [json.loads(l) for l in buf.getvalue().strip().split("\n")]
        error_record = lines[-1]
        self.assertIn("span/error: failing_op", error_record["msg"])
        self.assertIn("[redacted-openai-key]", error_record["payload"]["error"])

    def test_logger_listeners(self):
        events = []
        logger = TelemetryLogger(name="test", json_format=False, min_level="INFO")
        logger.add_listener(lambda rec: events.append(rec))
        logger.info("Test message", {"key": "value"})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["msg"], "Test message")
        self.assertEqual(events[0]["payload"]["key"], "value")


if __name__ == "__main__":
    unittest.main()
