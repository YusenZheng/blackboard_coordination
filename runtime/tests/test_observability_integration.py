from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from swarm_brain.coordination.policy import DeterministicGroupPlanningPolicy
from swarm_brain.memory import SQLiteMemoryStore
from swarm_brain.observability import (
    NoopObservability,
    ObservabilityHub,
    SQLiteObservationStore,
)
from swarm_brain.runtime.coordination_runtime import (
    CoordinationRuntime,
    _blackboard_display_attributes,
)
from swarm_brain.runtime.deepseek import DeepSeekConfig


EXPECTED_BUSINESS_EVENTS = [
    "task_posted",
    "bid_round_opened",
    "bid",
    "bid",
    "task_assigned",
    "action_intent",
    "receipt",
    "assignment_completed",
    "task_done",
]


class StaticIntentInterpreter:
    def interpret(self, raw_input: str, sequence: int) -> dict:
        return {
            "task_id": f"task_{sequence:03d}",
            "task_type": "search_target",
            "goal": raw_input,
            "success_condition": "目标被近距离确认",
            "priority": "high",
            "required_capability_ids": ["search"],
            "area": {"label": "公园", "area": "公园"},
            "role_slots": [
                {
                    "slot_id": "searcher",
                    "required_capability_ids": ["search"],
                    "exclusive": True,
                    "allowed_actions": ["move_to"],
                    "action_template": {
                        "verb": "move_to",
                        "params": {"target": "search-area"},
                        "reversible": True,
                    },
                    "completion_rule": {
                        "kind": "receipt_success",
                        "required_result_fields": [],
                    },
                }
            ],
        }


def _runtime(observability, memory=None) -> CoordinationRuntime:
    return CoordinationRuntime(
        deepseek_config=DeepSeekConfig(
            api_key="sk-" + "test-observability-fixture-1234567890"
        ),
        intent_interpreter=StaticIntentInterpreter(),
        local_policy_factory=lambda _device_id: None,
        group_policy=DeterministicGroupPlanningPolicy(),
        observability=observability,
        memory=memory,
    )


def _run_id(result: dict, hub: ObservabilityHub) -> str:
    direct = result.get("run_id")
    if direct:
        return str(direct)
    observation = result.get("observability", {})
    if isinstance(observation, dict) and observation.get("run_id"):
        return str(observation["run_id"])
    runs = hub.list_runs(limit=10)
    if len(runs) != 1:
        raise AssertionError(f"expected one persisted run, got {runs!r}")
    return str(runs[0]["run_id"])


def _business_projection(result: dict) -> dict:
    return {
        "status": result["status"],
        "task": {
            "task_id": result["task"]["task_id"],
            "status": result["task"]["status"],
            "winner": result["task"]["winner"],
            "completed_assignment_ids": result["task"]["completed_assignment_ids"],
        },
        "event_types": [
            item["type"] for item in result["blackboard"]["events"]
        ],
        "terminal_event_type": result["completion"]["terminal_event_type"],
        "terminal_status": result["completion"]["terminal_status"],
        "intent": {
            key: result["execution"]["intent"].get(key)
            for key in (
                "intent_id",
                "task_id",
                "device_id",
                "verb",
                "params",
            )
        },
        "receipt": {
            key: result["execution"]["receipt"].get(key)
            for key in (
                "intent_id",
                "device_id",
                "success",
                "outcome_certainty",
                "failure_reason",
            )
        },
    }


def _payload_ids(spans: list[dict], timeline: list[dict]) -> set[str]:
    values: set[str] = set()
    for span in spans:
        for key in ("input_payload_id", "output_payload_id"):
            if span.get(key):
                values.add(str(span[key]))
    for event in timeline:
        if event.get("payload_id"):
            values.add(str(event["payload_id"]))
    return values


class V2ObservabilityIntegrationTest(unittest.TestCase):
    def test_metadata_history_has_lifecycle_logs_and_safe_action_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hub = ObservabilityHub(
                Path(directory) / "observability.sqlite3",
                capture_mode="metadata",
            )
            try:
                result = _runtime(hub).run(
                    "帮我找公园里走失的白色萨摩耶"
                )
                self.assertTrue(hub.flush(5.0))
                run_id = _run_id(result, hub)

                logs = {
                    item["operation"]: item
                    for item in hub.query_logs(run_id)
                }
                self.assertIn("chain1.run.started", logs)
                self.assertIn("chain1.run.completed", logs)
                root = next(
                    item
                    for item in hub.query_trace(run_id)
                    if item["name"] == "swarm_brain.chain1.run"
                    and not item.get("parent_span_id")
                )
                for operation in (
                    "chain1.run.started",
                    "chain1.run.completed",
                ):
                    record = logs[operation]
                    self.assertEqual(run_id, record["run_id"])
                    self.assertEqual(result["trace_id"], record["trace_id"])
                    self.assertEqual(root["span_id"], record["span_id"])

                started = logs["chain1.run.started"]
                self.assertEqual("INFO", started["level"])
                self.assertEqual("running", started["attributes"]["status"])
                completed = logs["chain1.run.completed"]
                self.assertEqual("INFO", completed["level"])
                self.assertEqual("ok", completed["attributes"]["status"])
                self.assertGreaterEqual(
                    float(completed["attributes"]["duration_ms"]), 0.0
                )
                self.assertEqual(
                    result["task"]["task_id"],
                    completed["task_id"],
                )
                self.assertEqual(
                    result["task"]["task_id"],
                    completed["attributes"]["task_id"],
                )
                self.assertEqual(
                    result["task"]["winner"],
                    completed["attributes"]["winner"],
                )

                timeline = hub.query_timeline(run_id)
                committed = {
                    item["attributes"]["event.type"]: item
                    for item in timeline
                    if item["name"] == "blackboard.event.committed"
                    and item["attributes"].get("event.type")
                    in {"action_intent", "receipt"}
                }
                business = [
                    item
                    for item in timeline
                    if item["name"] == "blackboard.event.committed"
                ]
                task_posted = next(
                    item
                    for item in business
                    if item["attributes"]["event.type"] == "task_posted"
                )
                self.assertEqual(
                    "帮我找公园里走失的白色萨摩耶",
                    task_posted["attributes"]["task.goal_summary"],
                )
                bids = [
                    item
                    for item in business
                    if item["attributes"]["event.type"] == "bid"
                ]
                self.assertEqual(2, len(bids))
                for bid in bids:
                    self.assertIn(
                        bid["attributes"]["bid.decision"],
                        {"accept", "reject"},
                    )
                    self.assertIsInstance(
                        bid["attributes"]["bid.battery_margin"], float
                    )
                    self.assertIsInstance(
                        bid["attributes"]["bid.reliability"], float
                    )
                assignment = next(
                    item
                    for item in business
                    if item["attributes"]["event.type"] == "task_assigned"
                )
                self.assertEqual(
                    "dog-a",
                    assignment["attributes"]["assignment.device_id"],
                )
                action = committed["action_intent"]
                self.assertEqual(
                    {
                        "action.device_id": "dog-a",
                        "action.verb": "move_to",
                        "action.target": "search-area",
                    },
                    {
                        key: action["attributes"].get(key)
                        for key in (
                            "action.device_id",
                            "action.verb",
                            "action.target",
                        )
                    },
                )
                receipt = committed["receipt"]
                self.assertEqual(
                    "dog-a", receipt["attributes"]["receipt.device_id"]
                )
                self.assertIs(
                    True, receipt["attributes"]["receipt.success"]
                )
                self.assertEqual(
                    "confirmed",
                    receipt["attributes"]["receipt.outcome_certainty"],
                )
                self.assertIs(
                    True,
                    receipt["attributes"]["receipt.post_check_allowed"],
                )
                self.assertIsNone(
                    receipt["attributes"]["receipt.failure_reason"]
                )
                for item in committed.values():
                    payload = hub.get_payload(item["payload_id"])
                    self.assertIsNotNone(payload)
                    self.assertFalse(payload["content_available"])

                safety = _blackboard_display_attributes(
                    {
                        "type": "safety_intercept",
                        "content": {
                            "check_phase": "pre",
                            "risk_level": "R2",
                            "reason_code": "ACCESS_DENIED",
                            "reason": "restricted action",
                        },
                    }
                )
                self.assertEqual(
                    {
                        "safety.phase": "pre",
                        "safety.risk_level": "R2",
                        "safety.reason_code": "ACCESS_DENIED",
                        "safety.reason": "restricted action",
                    },
                    safety,
                )
            finally:
                hub.shutdown()

    def test_v2_run_persists_one_trace_nine_events_and_redacted_io(self) -> None:
        secrets = (
            "raw-password-123",
            "raw-cookie-456",
            "raw-url-token-789",
            "sk-raw-api-secret-abcdefgh",
        )
        instruction = (
            "帮我找公园里走失的白色萨摩耶；"
            f"password={secrets[0]} "
            f"Cookie={secrets[1]} "
            f"https://example.invalid/search?token={secrets[2]} "
            f"Authorization: Bearer {secrets[3]}"
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "observability.sqlite3"
            hub = ObservabilityHub(database, capture_mode="full")
            try:
                result = _runtime(hub).run(instruction)
                self.assertTrue(hub.flush(5.0))
                run_id = _run_id(result, hub)
                spans = hub.query_trace(run_id)
                timeline = hub.query_timeline(run_id)

                self.assertEqual("ok", result["status"])
                self.assertEqual(
                    EXPECTED_BUSINESS_EVENTS,
                    [item["type"] for item in result["blackboard"]["events"]],
                )
                self.assertTrue(spans)
                self.assertEqual(1, len({item["trace_id"] for item in spans}))
                roots = [
                    item for item in spans if not item.get("parent_span_id")
                ]
                self.assertEqual(
                    ["swarm_brain.chain1.run"],
                    [item["name"] for item in roots],
                )

                span_names = [item["name"] for item in spans]
                required_spans = {
                    "swarm_brain.chain1.run",
                    "ingress.task.generate",
                    "coordinator.bid_round.open",
                    "agent.process_bid_round",
                    "coordinator.group_plan",
                    "coordinator.plan.validate",
                    "safety.pre_check",
                    "tool.invoke",
                    "adapter.execute",
                    "safety.post_check",
                    "coordinator.complete",
                }
                self.assertTrue(
                    required_spans.issubset(span_names),
                    required_spans.difference(span_names),
                )
                self.assertEqual(2, span_names.count("agent.process_bid_round"))
                linked_group_spans = [
                    item
                    for item in spans
                    if item["name"] == "coordinator.group_plan"
                    and item.get("links")
                ]
                self.assertTrue(linked_group_spans)
                self.assertEqual(2, len(linked_group_spans[0]["links"]))

                blackboard_events = [
                    item
                    for item in timeline
                    if item["name"] == "blackboard.event.append"
                ]
                self.assertEqual(9, len(blackboard_events))
                self.assertEqual(
                    EXPECTED_BUSINESS_EVENTS,
                    [
                        item["attributes"]["event.type"]
                        for item in blackboard_events
                    ],
                )

                payload_ids = _payload_ids(spans, timeline)
                self.assertTrue(payload_ids)
                payloads = [
                    hub.get_payload(payload_id) for payload_id in payload_ids
                ]
                self.assertTrue(
                    any(
                        payload is not None
                        and payload.get("content_available")
                        for payload in payloads
                    )
                )
                persisted_payloads = json.dumps(
                    payloads,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                for secret in secrets:
                    self.assertNotIn(secret, persisted_payloads)
            finally:
                hub.shutdown()

            persisted = b""
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(database) + suffix)
                if path.exists():
                    persisted += path.read_bytes()
            for secret in secrets:
                self.assertNotIn(secret.encode("utf-8"), persisted)

    def test_sqlite_failure_and_noop_preserve_v2_business_result(self) -> None:
        instruction = "帮我找公园里走失的白色萨摩耶"
        baseline = _runtime(NoopObservability()).run(instruction)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked_parent = root / "not-a-directory"
            blocked_parent.write_text("blocks sqlite parent creation", encoding="utf-8")
            emergency_path = root / "logs" / "emergency.jsonl"
            store = SQLiteObservationStore(
                blocked_parent / "observability.sqlite3",
                emergency_path=emergency_path,
            )
            hub = ObservabilityHub(capture_mode="metadata", store=store)
            try:
                degraded = _runtime(hub).run(instruction)
                hub.flush(5.0)
                self.assertTrue(hub.health()["degraded"])
                self.assertGreater(hub.health()["write_errors"], 0)
            finally:
                hub.shutdown()

            self.assertTrue(emergency_path.exists())
            self.assertEqual(
                _business_projection(baseline),
                _business_projection(degraded),
            )
            self.assertEqual(EXPECTED_BUSINESS_EVENTS, _business_projection(degraded)["event_types"])

    def test_trace_asset_promotes_private_memory_and_review_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hub = ObservabilityHub(root / "observability.sqlite3")
            memory = SQLiteMemoryStore(root / "memory.sqlite3")
            try:
                result = _runtime(hub, memory).run(
                    "帮我找公园里走失的白色萨摩耶"
                )
                self.assertEqual("consolidated", result["memory"]["status"])
                self.assertEqual("approved", result["memory"]["quality_status"])
                self.assertTrue(
                    memory.search_private(
                        agent_id="dog-a",
                        task_id=result["task"]["task_id"],
                    )
                )
                self.assertTrue(
                    memory.search_private(
                        agent_id="dog-b",
                        task_id=result["task"]["task_id"],
                    )
                )
                candidates = memory.list_candidates()
                self.assertEqual(1, len(candidates))
                self.assertEqual("pending_review", candidates[0]["status"])
                self.assertEqual([], memory.read_facts(result["task"]["task_id"]))
                self.assertTrue(hub.flush(5.0))
                assets = hub.query_trace_assets(run_id=result["run_id"])
                self.assertEqual(1, len(assets))
                self.assertEqual("approved", assets[0]["quality_status"])
            finally:
                memory.close()
                hub.shutdown()

    def test_subscriber_exception_is_observed_and_does_not_stop_run(self) -> None:
        def broken_subscriber(_payload: dict) -> None:
            raise RuntimeError("dashboard subscriber unavailable")

        with tempfile.TemporaryDirectory() as directory:
            hub = ObservabilityHub(Path(directory) / "observability.sqlite3")
            try:
                result = _runtime(hub).run(
                    "帮我找公园里走失的白色萨摩耶",
                    status_listener=broken_subscriber,
                )
                self.assertEqual("ok", result["status"])
                self.assertTrue(hub.flush(5.0))
                errors = hub.query_errors(result["run_id"])
                self.assertTrue(
                    any(
                        item.get("name") == "subscriber.callback"
                        for item in errors
                    )
                )
            finally:
                hub.shutdown()


if __name__ == "__main__":
    unittest.main()
