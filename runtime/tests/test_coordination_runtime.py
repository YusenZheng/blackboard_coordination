from __future__ import annotations

import unittest

from swarm_brain.coordination.policy import DeterministicGroupPlanningPolicy
from swarm_brain.runtime.coordination_runtime import CoordinationRuntime
from swarm_brain.runtime.deepseek import DeepSeekConfig


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


class CoordinationRuntimeTest(unittest.TestCase):
    def test_one_command_runtime_reaches_task_done(self) -> None:
        runtime = CoordinationRuntime(
            deepseek_config=DeepSeekConfig(
                api_key="sk-" + "test-runtime-fixture-1234567890"
            ),
            intent_interpreter=StaticIntentInterpreter(),
            local_policy_factory=lambda _device_id: None,
            group_policy=DeterministicGroupPlanningPolicy(),
        )

        live_events: list[dict] = []
        live_statuses: list[dict] = []
        live_sessions: list[dict] = []
        live_tool_calls: list[dict] = []
        result = runtime.run(
            "帮我找公园里走失的白色萨摩耶",
            event_listener=live_events.append,
            status_listener=live_statuses.append,
            session_listener=live_sessions.append,
            tool_listener=live_tool_calls.append,
        )

        self.assertEqual("ok", result["status"])
        self.assertEqual(
            "whx_tool_runtime",
            result["runtime"]["physical_gateway"],
        )
        self.assertEqual(
            "asset_skill_reference_provider",
            result["runtime"]["skill_provider"],
        )
        self.assertEqual("done", result["task"]["status"])
        self.assertEqual("dog-a", result["task"]["winner"])
        self.assertEqual("task_done", result["completion"]["terminal_event_type"])
        self.assertTrue(
            all(result["completion"]["agent_task_sessions_cleared"].values())
        )
        self.assertEqual(9, result["blackboard"]["high_watermark"])
        self.assertEqual(
            [
                "task_posted",
                "bid_round_opened",
                "bid",
                "bid",
                "task_assigned",
                "action_intent",
                "receipt",
                "assignment_completed",
                "task_done",
            ],
            [item["type"] for item in result["blackboard"]["events"]],
        )
        self.assertEqual(
            [item["type"] for item in result["blackboard"]["events"]],
            [item["type"] for item in live_events],
        )
        self.assertEqual(list(range(1, 10)), [item["version"] for item in live_events])
        self.assertEqual("done", live_statuses[-1]["stage"])
        self.assertEqual(8, len(live_sessions))
        self.assertEqual(
            {"dog-a", "dog-b"},
            {item["device_id"] for item in live_sessions},
        )
        cleared = [
            item
            for item in live_sessions
            if item["phase"] == "after_task_done_cleanup"
        ]
        self.assertEqual(2, len(cleared))
        self.assertTrue(all(not item["exists"] for item in cleared))
        self.assertTrue(all(item["session"] is None for item in cleared))
        self.assertEqual(
            result["execution"]["tool_calls"],
            live_tool_calls,
        )
        self.assertEqual(1, len(live_tool_calls))
        self.assertEqual("G01", live_tool_calls[0]["tool_id"])
        self.assertTrue(live_tool_calls[0]["success"])
        self.assertEqual(
            "available",
            live_tool_calls[0]["binding"]["status"],
        )
        self.assertEqual(
            [result["execution"]["intent"]["intent_id"]],
            result["execution"]["dispatched_intent_ids_by_device"]["dog-a"],
        )
        self.assertEqual(
            [],
            result["execution"]["dispatched_intent_ids_by_device"]["dog-b"],
        )
        references = result["execution"]["skill_references_by_device"]
        self.assertTrue(references["dog-a"])
        self.assertTrue(references["dog-b"])
        self.assertIn(
            "safe_search_execution:battery_aware_navigation",
            {item["skill_id"] for item in references["dog-a"]},
        )
        self.assertEqual(
            references["dog-a"],
            live_tool_calls[0]["skill_references"],
        )


if __name__ == "__main__":
    unittest.main()
