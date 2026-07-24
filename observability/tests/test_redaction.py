from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from swarm_brain.observability.redaction import (
    OMITTED_REASONING,
    REDACTED,
    PayloadProcessor,
    redact_text,
    redact_value,
)
from swarm_brain.observability.storage import EmergencyJsonlWriter


class RedactionTest(unittest.TestCase):
    def test_recursive_redaction_and_hidden_reasoning(self) -> None:
        value = {
            "api_key": "sk-test-secret-123456",
            "nested": {
                "Authorization": "Bearer abc.def.ghi",
                "url": "https://example.test/path?token=url-secret&ok=1",
                "chain_of_thought": "private reasoning",
                ".env": "PUBLIC_VALUE=still-not-allowed",
            },
            "binary": b"\x00\x01\x02",
            "image": "data:image/png;base64,do-not-save-this",
        }

        safe = redact_value(value)

        self.assertEqual(REDACTED, safe["api_key"])
        self.assertEqual(REDACTED, safe["nested"]["Authorization"])
        self.assertIn("token=[REDACTED]", safe["nested"]["url"])
        self.assertEqual(
            OMITTED_REASONING, safe["nested"]["chain_of_thought"]
        )
        self.assertEqual(REDACTED, safe["nested"][".env"])
        self.assertEqual(3, safe["binary"]["byte_size"])
        self.assertEqual("[BINARY_DATA_URI_OMITTED]", safe["image"])
        self.assertNotIn("private reasoning", json.dumps(safe))

    def test_text_patterns_are_redacted(self) -> None:
        original = (
            "Authorization: Bearer abc123 Cookie=session=abcdef "
            "password=hunter2 DEEPSEEK_API_KEY=sk-secret-value-12345 "
            "https://example.test/?access_token=query-secret"
        )
        safe = redact_text(original)
        for secret in (
            "abc123",
            "session=abcdef",
            "hunter2",
            "sk-secret-value-12345",
            "query-secret",
        ):
            self.assertNotIn(secret, safe)

    def test_numeric_llm_token_usage_is_kept_but_token_secrets_are_redacted(
        self,
    ) -> None:
        safe = redact_value(
            {
                "input_tokens": 296,
                "output_tokens": 194,
                "total_tokens": 490,
                "cache_hit_tokens": 256,
                "token.kind": "input_tokens",
                "token": "credential-value",
                "access_token": "access-secret",
                "invalid_usage": {"input_tokens": "sk-secret-value-12345"},
            }
        )

        self.assertEqual(296, safe["input_tokens"])
        self.assertEqual(194, safe["output_tokens"])
        self.assertEqual(490, safe["total_tokens"])
        self.assertEqual(256, safe["cache_hit_tokens"])
        self.assertEqual("input_tokens", safe["token.kind"])
        self.assertEqual(REDACTED, safe["token"])
        self.assertEqual(REDACTED, safe["access_token"])
        self.assertEqual(REDACTED, safe["invalid_usage"]["input_tokens"])

    def test_payload_modes(self) -> None:
        metadata = PayloadProcessor("metadata").prepare(
            {"password": "secret"}, direction="input"
        )
        self.assertIsNotNone(metadata)
        self.assertFalse(metadata["content_available"])
        self.assertIsNone(metadata["redacted_content_json"])
        self.assertNotIn("secret", metadata["summary"])

        errors = PayloadProcessor("errors")
        self.assertIsNone(
            errors.prepare({"password": "secret"}, direction="input")
        )
        error_payload = errors.prepare(
            {"password": "secret"}, direction="input", is_error=True
        )
        self.assertTrue(error_payload["content_available"])
        self.assertNotIn("secret", error_payload["redacted_content_json"])

    def test_cyclic_and_large_payloads_are_bounded(self) -> None:
        cyclic: dict = {}
        cyclic["self"] = cyclic
        self.assertEqual("[CYCLE]", redact_value(cyclic)["self"])

        processor = PayloadProcessor("full", max_full_bytes=1024)
        prepared = processor.prepare(
            {"value": "\x00\\\"" * 10_000},
            direction="output",
        )
        self.assertTrue(prepared["is_truncated"])
        self.assertLessEqual(
            len(prepared["redacted_content_json"].encode("utf-8")), 1024
        )

    def test_emergency_jsonl_is_redacted_and_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "emergency.jsonl"
            writer = EmergencyJsonlWriter(
                path, max_bytes=180, backup_count=2
            )
            for index in range(10):
                self.assertTrue(
                    writer.write(
                        {
                            "message": (
                                f"{index} password=do-not-persist "
                                "Authorization: Bearer token-value"
                            )
                        }
                    )
                )
            persisted = b"".join(
                item.read_bytes()
                for item in path.parent.glob("emergency.jsonl*")
            )
            self.assertNotIn(b"do-not-persist", persisted)
            self.assertNotIn(b"token-value", persisted)
            self.assertTrue((path.parent / "emergency.jsonl.1").exists())


if __name__ == "__main__":
    unittest.main()
