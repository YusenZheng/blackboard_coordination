from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from swarm_brain.coordination.models import (
    BidMetrics,
    BidOffer,
    BidPayload,
    BidRecord,
    CollaborationProposal,
    GroupPlanningInput,
    RoleSlot,
)
from swarm_brain.ingress.task_gen import TaskGen, task_package_to_v2_content
from swarm_brain.runtime.deepseek import (
    DeepSeekClient,
    DeepSeekConfig,
    DeepSeekGroupPlanningPolicy,
    DeepSeekIntentInterpreter,
    DeepSeekLocalProposalPolicy,
)


class FakeTransport:
    def __init__(self, values: list[dict]):
        self.values = list(values)
        self.calls: list[dict] = []

    def post_json(self, url, headers, payload, timeout_s):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": payload,
                "timeout_s": timeout_s,
            }
        )
        content = json.dumps(self.values.pop(0), ensure_ascii=False)
        return {
            "id": "request-test-1",
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 4,
                "total_tokens": 16,
                "prompt_cache_hit_tokens": 3,
                "prompt_cache_miss_tokens": 9,
            },
        }

    def get_json(self, url, headers, timeout_s):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "timeout_s": timeout_s,
            }
        )
        return {"data": [{"id": "deepseek-v4-flash"}]}


def client_with(*values: dict) -> tuple[DeepSeekClient, FakeTransport]:
    transport = FakeTransport(list(values))
    client = DeepSeekClient(
        DeepSeekConfig(
            api_key="sk-" + "test-client-fixture-1234567890",
            model="deepseek-v4-flash",
        ),
        transport,
    )
    return client, transport


class DeepSeekConfigTest(unittest.TestCase):
    def test_config_reads_ignored_env_file_and_redacts_repr(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / ".env.local"
            test_key = "sk-" + "test-local-fixture-1234567890"
            env_file.write_text(
                f"DEEPSEEK_API_KEY={test_key}\n"
                "DEEPSEEK_MODEL=deepseek-v4-pro\n",
                encoding="utf-8",
            )
            config = DeepSeekConfig.from_env(env={}, env_file=env_file)
        self.assertEqual("deepseek-v4-pro", config.model)
        self.assertNotIn(test_key, repr(config))

    def test_client_uses_v4_endpoint_json_mode_without_exposing_key_in_payload(self):
        client, transport = client_with({"ok": True})
        telemetry = []
        client.telemetry_listener = telemetry.append
        result = client.chat_json(
            [
                {"role": "system", "content": "return json"},
                {"role": "user", "content": "hello"},
            ],
            operation="test_operation",
        )
        self.assertEqual({"ok": True}, result)
        call = transport.calls[0]
        self.assertEqual("https://api.deepseek.com/chat/completions", call["url"])
        self.assertEqual("deepseek-v4-flash", call["payload"]["model"])
        self.assertEqual({"type": "json_object"}, call["payload"]["response_format"])
        self.assertNotIn("sk-test-secret", json.dumps(call["payload"]))
        self.assertEqual("test_operation", telemetry[0]["operation"])
        self.assertEqual(12, telemetry[0]["input_tokens"])
        self.assertEqual(4, telemetry[0]["output_tokens"])
        self.assertEqual(16, telemetry[0]["total_tokens"])
        self.assertGreaterEqual(telemetry[0]["duration_ms"], 0)

    def test_client_lists_models_without_returning_credentials(self):
        client, _ = client_with()
        self.assertEqual(["deepseek-v4-flash"], client.list_models())


class DeepSeekPolicyTest(unittest.TestCase):
    def test_intent_interpreter_and_task_payload(self):
        client, _ = client_with(
            {
                "task_type": "search_target",
                "goal": "寻找走失的白色萨摩耶",
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
        )
        task = TaskGen(DeepSeekIntentInterpreter(client)).generate("帮我找狗")
        content = task_package_to_v2_content(task)
        self.assertEqual("task_001", task.task_id)
        self.assertEqual("search", content["role_slots"][0]["required_capability_ids"][0])
        self.assertEqual(2, content["schema_version"])

    def test_local_proposal_cannot_prefer_rejected_slot(self):
        client, _ = client_with(
            {
                "preferred_slot_ids": ["searcher", "forbidden"],
                "cooperation_summary": "承担地面搜索",
                "risk_notes": None,
                "evidence_refs": "agent_public_snapshot",
                "confidence": 0.8,
            }
        )
        proposal = DeepSeekLocalProposalPolicy(client).propose(
            {
                "context_fingerprint": "ctx-1",
                "hard_offers": [
                    {"slot_id": "searcher", "decision": "accept"},
                    {"slot_id": "forbidden", "decision": "reject"},
                ],
            },
            5.0,
        )
        self.assertIsNotNone(proposal)
        self.assertEqual(["searcher"], proposal.preferred_slot_ids)
        self.assertEqual([], proposal.risk_notes)
        self.assertEqual(["agent_public_snapshot"], proposal.evidence_refs)
        self.assertEqual("ctx-1", proposal.context_fingerprint)

    def test_group_policy_keeps_authoritative_task_metadata(self):
        client, _ = client_with(
            {
                "assignments": [
                    {
                        "slot_id": "searcher",
                        "device_id": "dog-a",
                        "supporting_bid_event_id": "bid-1",
                    }
                ],
                "rationale_summary": "dog-a 满足硬约束",
            }
        )
        metrics = BidMetrics(True, True, True, True, 0.5, 300, True, True, 10, 0.9)
        planning_input = GroupPlanningInput(
            task={"task_id": "task-1"},
            role_slots=[RoleSlot("searcher", ["search"], True, ["move_to"], {}, {})],
            bids=[
                BidRecord(
                    "bid-1",
                    1,
                    100.0,
                    BidPayload(
                        "task-1", 1, 1, 1, "dog-a",
                        [BidOffer("searcher", "accept", metrics)],
                        None,
                        110.0,
                    ),
                )
            ],
            proposals=[CollaborationProposal([], "", [], [], 0.5, "ctx")],
            agent_snapshots=[],
            evidence_view=None,
            task_revision=1,
            coordination_epoch=1,
            input_fingerprint="input-1",
        )
        plan = DeepSeekGroupPlanningPolicy(client).plan(planning_input, 5.0)
        self.assertEqual("task-1", plan.task_id)
        self.assertEqual("input-1", plan.input_fingerprint)
        self.assertEqual("dog-a", plan.assignments[0].device_id)


if __name__ == "__main__":
    unittest.main()
