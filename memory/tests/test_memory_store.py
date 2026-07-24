from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from swarm_brain.memory import (
    Provenance,
    SQLiteMemoryStore,
    TaskEpisodeBuilder,
    TraceAssetStatus,
)


class SQLiteMemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.store = SQLiteMemoryStore(self.path)
        self.provenance = Provenance(
            "trace-1",
            "span-1",
            ("event-1",),
            "confirmed_receipt",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_wal_schema_and_fts5_are_enabled(self) -> None:
        health = self.store.health()
        self.assertEqual("wal", health["journal_mode"])
        self.assertTrue(health["foreign_keys"])
        self.assertTrue(health["fts5"])
        self.assertEqual(1, health["schema_version"])

    def test_private_memories_are_agent_scoped_and_searchable(self) -> None:
        self.store.put_private(
            "dog-a",
            "episodic",
            {"content": {"summary": "searched the eastern bushes"}, "task_id": "t1"},
            provenance=self.provenance,
            confidence=0.9,
        )
        self.store.put_private(
            "dog-b",
            "episodic",
            {"content": {"summary": "searched the western garage"}, "task_id": "t1"},
            provenance=self.provenance,
            confidence=0.9,
        )
        own = self.store.search_private(
            agent_id="dog-a", kind="episodic", text="eastern", limit=3
        )
        other = self.store.search_private(
            agent_id="dog-b", kind="episodic", text="eastern", limit=3
        )
        self.assertEqual(1, len(own))
        self.assertEqual("dog-a", own[0]["agent_id"])
        self.assertEqual([], other)

    def test_expired_retired_and_superseded_records_are_not_retrieved(self) -> None:
        old = self.store.put_private(
            "dog-a",
            "episodic",
            {"content": {"summary": "old route"}},
            provenance=self.provenance,
        )
        self.store.put_private(
            "dog-a",
            "episodic",
            {"content": {"summary": "new route"}},
            provenance=self.provenance,
            supersedes_id=old["id"],
        )
        self.store.put_private(
            "dog-a",
            "episodic",
            {"content": {"summary": "expired route"}},
            provenance=self.provenance,
            expires_at=time.time() - 1,
        )
        visible = self.store.search_private(agent_id="dog-a", limit=10)
        self.assertEqual(["new route"], [item["content"]["summary"] for item in visible])

    def test_fact_requires_review_and_authoritative_source(self) -> None:
        candidate = self.store.submit_fact_candidate(
            topic="garage",
            fact={"statement": "low radio coverage"},
            provenance=self.provenance,
            confidence=0.8,
            source_type="confirmed_receipt",
        )
        self.assertEqual([], self.store.read_facts("garage"))
        decided = self.store.decide_candidate(
            candidate["id"], "approve", decided_by="operator"
        )
        self.assertEqual("approved", decided["status"])
        facts = self.store.read_facts("garage")
        self.assertEqual(1, len(facts))
        self.assertEqual("low radio coverage", facts[0]["fact"]["statement"])

        untrusted = self.store.submit_fact_candidate(
            topic="garage",
            fact={"statement": "LLM guessed a tunnel"},
            provenance=Provenance("trace-2", "span-2", ("event-2",), "llm_text"),
            confidence=0.7,
            source_type="llm_text",
        )
        with self.assertRaises(ValueError):
            self.store.decide_candidate(
                untrusted["id"], "approve", decided_by="operator"
            )

    def test_rejected_candidate_never_enters_fact_search(self) -> None:
        candidate = self.store.submit_fact_candidate(
            topic="weather",
            fact={"statement": "rain"},
            provenance=self.provenance,
            confidence=0.8,
            source_type="confirmed_receipt",
        )
        self.store.decide_candidate(
            candidate["id"], "reject", decided_by="operator", reason="bad sensor"
        )
        self.assertEqual([], self.store.search_facts(text="rain"))
        self.assertEqual(
            "rejected",
            self.store.get_candidate(candidate["id"])["status"],
        )

    def test_storage_redacts_credentials(self) -> None:
        record = self.store.put_private(
            "dog-a",
            "episodic",
            {
                "content": {
                    "authorization": "Bearer secret-value",
                    "url": "https://example.test/?token=secret-value",
                    "note": "uses sk-1234567890abcdef",
                }
            },
            provenance=self.provenance,
        )
        serialized = str(record)
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("1234567890abcdef", serialized)
        raw = self.path.read_bytes()
        self.assertNotIn(b"secret-value", raw)
        self.assertNotIn(b"1234567890abcdef", raw)

    def test_binary_and_hidden_reasoning_are_never_persisted(self) -> None:
        record = self.store.put_private(
            "dog-a",
            "episodic",
            {
                "content": {
                    "image": b"raw-binary-body",
                    "chain_of_thought": "private hidden reasoning",
                }
            },
            provenance=self.provenance,
        )
        self.assertTrue(record["content"]["image"]["binary_omitted"])
        self.assertEqual("[REDACTED]", record["content"]["chain_of_thought"])
        database = self.path.read_bytes()
        self.assertNotIn(b"raw-binary-body", database)
        self.assertNotIn(b"private hidden reasoning", database)

    def test_working_memory_can_be_cleaned_at_task_terminal(self) -> None:
        self.store.put_private(
            "dog-a",
            "working",
            {"task_id": "t1", "content": {"step": 1}},
            provenance=self.provenance,
        )
        self.store.put_private(
            "dog-a",
            "working",
            {"task_id": "t2", "content": {"step": 2}},
            provenance=self.provenance,
        )
        self.assertEqual(1, self.store.clear_working_memory(task_id="t1"))
        remaining = self.store.retrieve_private(
            "dog-a", "working", {"limit": 10}
        )
        self.assertEqual(["t2"], [item["task_id"] for item in remaining])

    def test_context_budget_and_shared_only_boundary(self) -> None:
        self.store.put_private(
            "dog-a",
            "episodic",
            {"content": {"summary": "garage route memory"}},
            provenance=self.provenance,
        )
        fact_candidate = self.store.submit_fact_candidate(
            topic="garage",
            fact={"statement": "garage route is narrow"},
            provenance=self.provenance,
            confidence=0.9,
            source_type="confirmed_receipt",
        )
        self.store.decide_candidate(
            fact_candidate["id"], "approve", decided_by="operator"
        )
        local = self.store.retrieve_context(
            agent_id="dog-a", query="garage", max_chars=120
        )
        self.assertLessEqual(local["character_count"], 120)
        self.assertEqual(1, len(local["private_episodes"]))
        group = self.store.retrieve_shared_context("garage", max_chars=120)
        self.assertEqual([], group["private_episodes"])
        self.assertEqual(1, len(group["shared_facts"]))


class ConsolidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteMemoryStore(Path(self.temp.name) / "memory.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def _trace() -> dict:
        return {
            "run_id": "run-1",
            "task_id": "task-1",
            "trace_id": "trace-1",
            "events": [
                {
                    "event_id": "event-1",
                    "event_type": "task_posted",
                    "span_id": "span-root",
                    "offset": 1,
                },
                {
                    "event_id": "event-2",
                    "event_type": "bid",
                    "span_id": "span-bid",
                    "agent_id": "dog-a",
                    "offset": 2,
                },
                {
                    "event_id": "event-3",
                    "event_type": "receipt",
                    "span_id": "span-tool",
                    "agent_id": "dog-a",
                    "offset": 3,
                },
                {
                    "event_id": "event-4",
                    "event_type": "task_done",
                    "span_id": "span-root",
                    "offset": 4,
                },
            ],
            "spans": [
                {
                    "span_id": "span-root",
                    "parent_span_id": None,
                    "start_time": 1.0,
                    "end_time": 4.0,
                },
                {
                    "span_id": "span-bid",
                    "parent_span_id": "span-root",
                    "start_time": 2.0,
                    "end_time": 2.5,
                },
            ],
        }

    def test_qualified_trace_creates_private_episode_and_pending_candidates(self) -> None:
        asset = TaskEpisodeBuilder().from_trace(self._trace())
        result = self.store.consolidate_trace_asset(
            asset,
            fact_candidates=[
                {
                    "topic": "route",
                    "fact": {"statement": "east route confirmed"},
                    "source_type": "confirmed_receipt",
                    "confidence": 0.9,
                },
                {
                    "topic": "guess",
                    "fact": {"statement": "free-form guess"},
                    "source_type": "llm_text",
                },
            ],
            skill_candidates=[
                {
                    "topic": "search",
                    "skill": {"title": "fan search"},
                    "confidence": 0.8,
                }
            ],
        )
        self.assertEqual(TraceAssetStatus.APPROVED, result.quality_status)
        self.assertEqual(1, len(result.private_memory_ids))
        self.assertEqual(2, len(result.candidate_ids))
        memories = self.store.search_private(agent_id="dog-a", limit=3)
        self.assertEqual("trace-1", memories[0]["source_trace_id"])
        self.assertEqual([], self.store.read_facts("route"))
        pending = self.store.list_candidates()
        self.assertEqual({"fact", "skill"}, {item["candidate_type"] for item in pending})

        duplicate = self.store.consolidate_trace_asset(asset)
        self.assertTrue(duplicate.deduplicated)
        self.assertEqual(result.private_memory_ids, duplicate.private_memory_ids)

    def test_failed_task_can_be_a_qualified_episode(self) -> None:
        trace = self._trace()
        trace["events"][-1] = {
            "event_id": "event-4",
            "event_type": "task_failed",
            "span_id": "span-root",
            "offset": 4,
        }
        asset = TaskEpisodeBuilder().from_trace(trace)
        result = self.store.consolidate_trace_asset(asset)
        self.assertEqual(TraceAssetStatus.APPROVED, result.quality_status)
        memory = self.store.search_private(agent_id="dog-a", limit=1)[0]
        self.assertEqual("failed", memory["content"]["outcome"])

    def test_trace_with_secret_is_rejected_without_persistence(self) -> None:
        trace = self._trace()
        trace["events"][0]["authorization"] = "Bearer raw-secret"
        asset = TaskEpisodeBuilder().from_trace(trace)
        result = self.store.consolidate_trace_asset(asset)
        self.assertEqual(TraceAssetStatus.REJECTED, result.quality_status)
        self.assertIn("sensitive_payload_detected", result.reasons)
        self.assertEqual([], self.store.search_private(agent_id="dog-a", limit=3))
        self.assertNotIn(b"raw-secret", self.store.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
