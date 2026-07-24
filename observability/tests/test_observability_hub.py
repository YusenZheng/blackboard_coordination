from __future__ import annotations

import logging
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from swarm_brain.observability import (
    NoopObservability,
    ObservabilityHub,
    ObservabilityPort,
)
from swarm_brain.observability.storage import SQLiteObservationStore


class ObservabilityHubTest(unittest.TestCase):
    def test_run_trace_event_log_metric_and_payload_queries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "observability.sqlite3"
            hub = ObservabilityHub(database, capture_mode="metadata")
            logger = logging.getLogger(f"observation-test-{id(hub)}")
            logger.propagate = False
            hub.configure_logging(logger)
            run_id = hub.start_run()
            hub.update_run(
                run_id,
                task_id="task-001",
                task_revision=2,
                coordination_epoch=3,
                attributes={"intent": "search"},
            )
            with hub.span(
                "swarm_brain.chain1.run",
                {"task.id": "task-001"},
                input_payload={
                    "Authorization": "Bearer abc-secret-token",
                    "url": "https://example.test/?token=query-secret",
                },
            ) as root:
                carrier = hub.current_carrier()
                self.assertRegex(
                    carrier.get("traceparent", ""),
                    r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$",
                )
                with hub.span("agent.process_bid_round") as bid:
                    bid_context = dict(bid.span_context)
                    hub.event(
                        "blackboard.event.append",
                        {
                            "category": "blackboard",
                            "event_id": "bb-event-1",
                            "offset": 1,
                        },
                        payload={"Cookie": "session=secret-cookie"},
                    )
                    hub.counter(
                        "swarm_brain.blackboard.events",
                        attributes={
                            "event_type": "task_posted",
                            "run_id": run_id,
                            "swarm.run.id": run_id,
                        },
                    )
                    hub.histogram(
                        "swarm_brain.agent.bid.duration",
                        12.5,
                        {"agent_kind": "robot"},
                    )
                    logger.info(
                        "bid complete password=log-secret",
                        extra={"operation": "agent.bid"},
                    )
                with hub.span(
                    "coordinator.group_plan", links=[bid_context]
                ) as group:
                    self.assertTrue(group.span_context)
                    group.set_output({"winner": "dog-a"})
                root.set_output({"status": "done"})
                self.assertTrue(
                    hub.save_trace_asset(
                        {
                            "asset_id": "asset-001",
                            "task_id": "task-001",
                            "root_span_id": root.span_context["span_id"],
                            "terminal_status": "done",
                            "span_ids": [root.span_context["span_id"]],
                            "event_refs": [
                                {"event_id": "bb-event-1", "event_type": "receipt"}
                            ],
                            "summary": "safe episode",
                            "redaction_clean": True,
                            "quality_status": "pending_review",
                        }
                    )
                )
            hub.end_run(run_id, status="done")
            self.assertTrue(hub.flush(5.0))

            run = hub.get_run(run_id)
            self.assertEqual("done", run["status"])
            self.assertEqual("task-001", run["task_id"])
            self.assertEqual(2, run["task_revision"])
            self.assertEqual("search", run["attributes"]["intent"])
            self.assertRegex(run["trace_id"], r"^[0-9a-f]{32}$")
            spans = hub.query_trace(run_id)
            self.assertEqual(3, len(spans))
            root_rows = [
                item
                for item in spans
                if item["name"] == "swarm_brain.chain1.run"
            ]
            self.assertEqual(1, len(root_rows))
            self.assertIsNone(root_rows[0]["parent_span_id"])
            group_rows = [
                item
                for item in spans
                if item["name"] == "coordinator.group_plan"
            ]
            self.assertEqual(1, len(group_rows[0]["links"]))
            timeline = hub.query_timeline(run_id)
            self.assertTrue(
                any(
                    item["name"] == "blackboard.event.append"
                    for item in timeline
                )
            )
            logs = hub.query_logs(run_id)
            self.assertTrue(any(item["operation"] == "agent.bid" for item in logs))
            metrics = hub.query_metrics(run_id)
            self.assertTrue(
                any(
                    item["metric_name"]
                    == "swarm_brain.blackboard.events"
                    for item in metrics
                )
            )
            metric = next(
                item
                for item in metrics
                if item["metric_name"] == "swarm_brain.blackboard.events"
            )
            self.assertNotIn("run_id", metric["attributes"])
            self.assertNotIn("swarm.run.id", metric["attributes"])
            payloads = [
                item
                for item in (
                    hub.get_payload(span.get("input_payload_id"))
                    for span in spans
                    if span.get("input_payload_id")
                )
                if item
            ]
            self.assertTrue(payloads)
            self.assertTrue(
                all(not item["content_available"] for item in payloads)
            )
            self.assertFalse(hub.health()["degraded"])
            assets = hub.query_trace_assets(run_id=run_id)
            self.assertEqual("pending_review", assets[0]["quality_status"])
            self.assertEqual("safe episode", assets[0]["episode"]["summary"])
            self.assertEqual(
                ["bb-event-1"], assets[0]["source_event_ids"]
            )
            self.assertTrue(hub.shutdown())

            persisted = database.read_bytes()
            wal_path = Path(str(database) + "-wal")
            if wal_path.exists():
                persisted += wal_path.read_bytes()
            for secret in (
                b"abc-secret-token",
                b"query-secret",
                b"secret-cookie",
                b"log-secret",
            ):
                self.assertNotIn(secret, persisted)

    def test_error_mode_only_keeps_error_payload_and_records_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hub = ObservabilityHub(
                Path(directory) / "observability.sqlite3",
                capture_mode="errors",
            )
            run_id = hub.start_run(task_id="task-error")
            with hub.span(
                "successful.step", input_payload={"value": "not retained"}
            ):
                pass
            with self.assertRaisesRegex(ValueError, "password"):
                with hub.span(
                    "failed.step",
                    input_payload={"password": "never-write-this"},
                ):
                    raise ValueError("password=exception-secret")
            hub.end_run(run_id, status="failed")
            self.assertTrue(hub.flush(5.0))

            spans = hub.query_trace(run_id)
            success = next(item for item in spans if item["name"] == "successful.step")
            failed = next(item for item in spans if item["name"] == "failed.step")
            self.assertIsNone(success["input_payload_id"])
            self.assertEqual("ERROR", failed["status_code"])
            payload = hub.get_payload(failed["input_payload_id"])
            self.assertTrue(payload["content_available"])
            self.assertNotIn("never-write-this", str(payload["redacted_content"]))
            self.assertTrue(hub.query_errors(run_id))
            hub.shutdown()

    def test_database_pragmas_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "observability.sqlite3"
            hub = ObservabilityHub(database)
            self.assertTrue(hub.flush())
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    "wal", connection.execute("PRAGMA journal_mode").fetchone()[0]
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertTrue(
                {
                    "runs",
                    "spans",
                    "events",
                    "logs",
                    "metric_points",
                    "payloads",
                    "trace_assets",
                    "schema_migrations",
                }.issubset(tables)
            )
            hub.shutdown()

    def test_consumer_context_continues_w3c_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hub = ObservabilityHub(Path(directory) / "observability.sqlite3")
            run_id = hub.start_run(task_id="task-propagation")
            with hub.span("producer") as producer:
                producer_context = dict(producer.span_context)
                carrier = hub.current_carrier()
            with hub.consumer_context(carrier):
                with hub.span("consumer") as consumer:
                    consumer_context = dict(consumer.span_context)
            with hub.consumer_context({"traceparent": "invalid"}):
                with hub.span("invalid-carrier"):
                    pass
            hub.event("business.append", {"event_id": "same-business-event"})
            hub.event("business.consume", {"event_id": "same-business-event"})
            hub.end_run(run_id)
            self.assertTrue(hub.flush(5.0))
            spans = {item["name"]: item for item in hub.query_trace(run_id)}
            self.assertEqual(
                producer_context["trace_id"], consumer_context["trace_id"]
            )
            self.assertEqual(
                producer_context["span_id"], spans["consumer"]["parent_span_id"]
            )
            correlated = [
                item
                for item in hub.query_timeline(run_id)
                if item["attributes"].get("event_id")
                == "same-business-event"
            ]
            self.assertEqual(2, len(correlated))
            hub.shutdown()

    def test_noop_satisfies_port(self) -> None:
        observer = NoopObservability()
        self.assertIsInstance(observer, ObservabilityPort)
        run_id = observer.start_run()
        with observer.span("anything") as span:
            span.set_output({"ok": True})
            self.assertEqual({}, span.span_context)
        observer.end_run(run_id)
        self.assertTrue(observer.flush())

    def test_sqlite_failure_degrades_to_redacted_emergency_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            invalid_database = Path(directory) / "database-is-a-directory"
            invalid_database.mkdir()
            store = SQLiteObservationStore(
                invalid_database,
                queue_capacity=2,
                busy_timeout_ms=50,
            )
            started = time.monotonic()
            self.assertTrue(
                store.enqueue(
                    "run_start",
                    {
                        "run_id": "run-degraded",
                        "status": "running",
                        "capture_mode": "metadata",
                        "api_key": "sk-must-not-survive-12345",
                    },
                    priority="critical",
                )
            )
            # 入队本身不等待 SQLite 重试。
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertTrue(store.flush(5.0))
            self.assertTrue(store.health()["degraded"])
            self.assertTrue(store.close(2.0))
            emergency = (
                invalid_database.parent / "logs" / "emergency.jsonl"
            ).read_text(encoding="utf-8")
            self.assertNotIn("sk-must-not-survive-12345", emergency)


if __name__ == "__main__":
    unittest.main()
