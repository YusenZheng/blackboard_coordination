from __future__ import annotations

import unittest

from swarm_brain.access.adapters.mock_adapter import MockAdapter
from swarm_brain.access.capability_matcher import CapabilityMatcher
from swarm_brain.access.registry import Registry
from swarm_brain.access.tool_gateway import ToolGateway
from swarm_brain.access.tools.base import load_builtin_tools
from swarm_brain.assets.emergence import CandidateLifecycle, CandidateState
from swarm_brain.assets.skill import Skill, SkillCandidate, load_builtin_skills
from swarm_brain.contracts.agent_card import AgentCard, CapabilitySlot
from swarm_brain.contracts.task import ActionIntent
from swarm_brain.contracts.tooling import CapabilityRequirement, ToolCallRequest
from swarm_brain.contracts.types import CapabilityProfile, DeviceRef, DeviceState, DeviceType
from swarm_brain.contracts.verbs import ActionVerb
from swarm_brain.coordination.models import SkillQuery
from swarm_brain.runtime.skill_reference_provider import AssetSkillReferenceProvider


def make_card(device_id: str = "dog-a", capabilities=None) -> AgentCard:
    return AgentCard(
        identity=DeviceRef(device_id, DeviceType.DOG),
        state=DeviceState(battery=0.9, online=True, healthy=True),
        capability=CapabilitySlot(
            action_verbs=[ActionVerb.MOVE_TO],
            profile=CapabilityProfile(capabilities=list(capabilities or ["search"]), width_cm=40),
        ),
    )


class ToolSkillMvpTest(unittest.TestCase):
    def test_capability_match_is_explainable(self) -> None:
        result = CapabilityMatcher().match(
            make_card(), CapabilityRequirement(all_of=frozenset({"sensor:thermal"}))
        )
        self.assertFalse(result.matched)
        self.assertEqual(["sensor:thermal"], result.missing_capabilities)
        self.assertIn("REQUIRED_CAPABILITY_MISSING", result.reason_codes)

    def test_g01_dispatch_binds_agent_card_and_emits_trace(self) -> None:
        registry = Registry()
        registry.register(make_card())
        traces: list[dict] = []
        gateway = ToolGateway(
            adapters={"dog-a": MockAdapter("dog-a")},
            device_registry=registry,
            trace_listener=traces.append,
        )
        intent = ActionIntent(
            intent_id="intent-1", device_id="dog-a", verb=ActionVerb.MOVE_TO,
            params={"target": "search-area"}, task_id="task-1",
            extra={"skill_references": [{"skill_id": "search-skill"}]},
        )
        receipt = gateway.dispatch(intent)
        self.assertTrue(receipt.success)
        self.assertEqual("G01", receipt.extra["tool_id"])
        self.assertEqual("available", traces[0]["binding"]["status"])
        self.assertEqual("search-skill", traces[0]["skill_references"][0]["skill_id"])

    def test_schema_alias_and_idempotency(self) -> None:
        gateway = ToolGateway()
        invalid = gateway.invoke(ToolCallRequest("2.0", "bad", "S11", {}, idempotency_key="bad"))
        self.assertEqual("INVALID_ARGUMENT", invalid.error_code)
        first = gateway.invoke(ToolCallRequest("2.0", "call-1", "S11", {"battery": 0.8}, idempotency_key="same"))
        replay = gateway.invoke(ToolCallRequest("2.0", "call-2", "S11", {"battery": 0.8}, idempotency_key="same"))
        conflict = gateway.invoke(ToolCallRequest("2.0", "call-3", "S11", {"battery": 0.1}, idempotency_key="same"))
        self.assertTrue(first.success)
        self.assertEqual(first.result, replay.result)
        self.assertEqual("IDEMPOTENCY_CONFLICT", conflict.error_code)
        self.assertEqual("C03", gateway.catalog.canonical_id("V02"))

    def test_skill_registry_returns_validated_references(self) -> None:
        catalog = load_builtin_tools()
        provider = AssetSkillReferenceProvider(load_builtin_skills(), catalog)
        references = provider.search(SkillQuery(task_type="search_target", capability_ids=["search"]))
        self.assertEqual(2, len(references))
        self.assertTrue(all(reference.tool_chain for reference in references))
        self.assertTrue(all(set(reference.tool_chain) <= {"G01", "S11", "C03", "Y06"} for reference in references))

    def test_emergence_candidate_lifecycle_rejects_skipped_admission(self) -> None:
        candidate = SkillCandidate(skill=Skill(signature="candidate-1"))
        lifecycle = CandidateLifecycle()
        lifecycle.transition(candidate, CandidateState.CANDIDATE_TRIAL)
        lifecycle.transition(candidate, CandidateState.VALIDATED)
        lifecycle.transition(candidate, CandidateState.REGISTERED)
        self.assertEqual("registered", candidate.lifecycle_status)
        with self.assertRaises(ValueError):
            lifecycle.transition(candidate, CandidateState.VALIDATED)


if __name__ == "__main__":
    unittest.main()
