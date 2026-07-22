# STATUS: STAGED(A类)—— 05/06 协同合同核心路径测试
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from swarm_brain.contracts.blackboard_event import Ledger
from swarm_brain.coordination.action_executor import ActionExecutor, DeterministicActionPolicy
from swarm_brain.coordination.adapters import (
    MockPhysicalActionGateway,
    NullSkillReferenceProvider,
    StaticSafetyPort,
)
from swarm_brain.coordination.agent_loop import PureAgentLoop
from swarm_brain.coordination.agent_process import AgentProcessHost
from swarm_brain.coordination.blackboard_client import (
    BlackboardClient,
    BlackboardContractError,
    ViewNotCaughtUpError,
)
from swarm_brain.coordination.coordinator import ActiveBidRound, Coordinator
from swarm_brain.coordination.file_session_store import FileCoordinatorStore, FileTaskSessionStore
from swarm_brain.coordination.models import (
    AgentPublicSnapshot,
    AgentProcessSpec,
    AppendResult,
    AppendStatus,
    BidRecord,
    CandidateAssignment,
    CandidateAssignmentPlan,
    CoordinationEventType,
    EventEnvelope,
    ExecutionAvailability,
    GroupPlanningInput,
    LoopInput,
    RiskLevel,
    RoleSlot,
    SafetyVerdict,
    TaskSession,
    ValidPlan,
    deterministic_event_id,
    event_type_value,
    fingerprint,
    make_blackboard_event,
    to_json_value,
)
from swarm_brain.coordination.policy import (
    EligibilityBidEngine,
    MinimalPlanValidator,
    deterministic_maximum_matching,
)
from swarm_brain.coordination.publisher import (
    DurableEventPublisher,
    PermanentAppendError,
)


def make_slot(slot_id: str, capability: str) -> RoleSlot:
    return RoleSlot(
        slot_id=slot_id,
        required_capability_ids=[capability],
        exclusive=True,
        allowed_actions=["move_to"],
        action_template={"verb": "move_to", "params": {"target": slot_id}, "reversible": True},
        completion_rule={"kind": "receipt_success", "required_result_fields": []},
    )


def make_snapshot(device_id: str, capability: str, version: int = 1) -> AgentPublicSnapshot:
    return AgentPublicSnapshot(
        device_id=device_id,
        device_type="dog",
        online=True,
        healthy=True,
        busy=False,
        busy_task_id=None,
        battery=0.9,
        endurance_s=600,
        position=None,
        current_action="idle",
        capabilities=[capability],
        action_verbs=["move_to"],
        constraints=[],
        width_cm=40,
        lowest_posture_cm=None,
        step_limit_cm=None,
        payload_kg=None,
        success_rate=0.95,
        card_version=version,
        state_updated_at=100,
        view_version=version,
    )


def make_task(slots: list[RoleSlot]) -> dict:
    return {
        "schema_version": 2,
        "task_id": "task-1",
        "task_revision": 1,
        "task_type": "search",
        "goal": "search target",
        "requirement": {
            "required_capabilities": sorted(
                {cap for slot in slots for cap in slot.required_capability_ids}
            ),
            "min_battery": 0.2,
            "min_endurance_s": 60,
            "space_constraints": [],
        },
        "role_slots": to_json_value(slots),
    }


class RecordingBlackboard:
    def __init__(self) -> None:
        self.events = []
        self.clock = 100.0
        self.views = {}

    def now(self):
        return self.clock

    def high_watermark(self):
        return len(self.events)

    def append(self, event):
        event.version = len(self.events) + 1
        event.ts = self.clock
        self.events.append(event)
        return AppendResult(
            AppendStatus.APPENDED,
            len(self.events) - 1,
            event.version,
            event.id,
        )

    def read_since(self, offset, event_types, limit, timeout_s):
        return []

    def query_view(self, ledger, filt, min_version, timeout_s):
        key = filt["view_type"]
        value = self.views[key]
        return value(filt) if callable(value) else value


class HistoryBlackboard(RecordingBlackboard):
    def __init__(self, history):
        super().__init__()
        self.history = list(history)

    def high_watermark(self):
        return max((item.event.version for item in self.history), default=0)

    def read_since(self, offset, event_types, limit, timeout_s):
        return [
            item
            for item in self.history
            if item.offset > offset and event_type_value(item.event) in event_types
        ][:limit]


def make_single_slot_planning_board():
    slot = make_slot("search", "search")
    task = make_task([slot])
    snapshot = make_snapshot("dog-a", "search")
    bid = EligibilityBidEngine().make_bid(
        task=task,
        role_slots=[slot],
        snapshot=snapshot,
        execution_availability=[ExecutionAvailability("dog-a", "move_to", True, 100)],
        task_revision=1,
        coordination_epoch=1,
        bid_round=1,
        deadline=110,
        now=100,
        proposal=None,
    )
    board = RecordingBlackboard()
    board.clock = 111
    board.views["task_coordination"] = {
        "task_id": "task-1",
        "task_revision": 1,
        "task": task,
        "status": "bidding",
        "coordination_epoch": 1,
        "terminal_event_id": None,
        "replan_pending": False,
        "view_version": 1,
    }
    board.views["bid_round"] = {
        "bids_by_device": {
            "dog-a": {
                "event_id": "bid-1",
                "event_version": 1,
                "event_ts": 100,
                "payload": to_json_value(bid),
            }
        }
    }
    board.views["agent_public"] = {"dog-a": to_json_value(snapshot)}
    board.views["evidence"] = {}
    return board, ActiveBidRound("task-1", 1, 1, 1, 110)


class EligibilityAndPlanningTest(unittest.TestCase):
    def test_hard_bid_and_complete_matching(self):
        slots = [make_slot("search", "search"), make_slot("confirm", "confirm")]
        task = make_task(slots)
        engine = EligibilityBidEngine()
        snapshots = [make_snapshot("dog-a", "search"), make_snapshot("dog-b", "confirm")]
        records = []
        for index, snapshot in enumerate(snapshots, start=1):
            payload = engine.make_bid(
                task=task,
                role_slots=slots,
                snapshot=snapshot,
                execution_availability=[
                    ExecutionAvailability(snapshot.device_id, "move_to", True, 100)
                ],
                task_revision=1,
                coordination_epoch=1,
                bid_round=1,
                deadline=110,
                now=100,
                proposal=None,
            )
            records.append(BidRecord(f"bid-{index}", index, 100, payload))

        base = {
            "task": task,
            "role_slots": slots,
            "bids": records,
            "agent_snapshots": snapshots,
        }
        planning = GroupPlanningInput(
            task=task,
            role_slots=slots,
            bids=records,
            proposals=[],
            agent_snapshots=snapshots,
            evidence_view=None,
            task_revision=1,
            coordination_epoch=1,
            input_fingerprint=fingerprint(base),
        )
        candidate = deterministic_maximum_matching(planning)
        self.assertIsNotNone(candidate)
        validation = MinimalPlanValidator().validate(candidate, planning)
        self.assertIsInstance(validation, ValidPlan)
        self.assertEqual(2, len(validation.assignment_plan.assignments))

    def test_stale_agent_is_rejected(self):
        slot = make_slot("search", "search")
        snapshot = make_snapshot("dog-a", "search")
        offer = EligibilityBidEngine(agent_state_ttl_s=30).evaluate_slot(
            task=make_task([slot]),
            slot=slot,
            snapshot=snapshot,
            execution_availability=[ExecutionAvailability("dog-a", "move_to", True, 200)],
            now=200,
        )
        self.assertEqual("reject", offer.decision)
        self.assertIn("STALE_AGENT_STATE", offer.reason_codes)

    def test_unsupported_action_is_not_mislabeled_as_channel_outage(self):
        slot = make_slot("search", "search")
        slot.action_template["verb"] = "hover"
        snapshot = make_snapshot("dog-a", "search")
        offer = EligibilityBidEngine().evaluate_slot(
            task=make_task([slot]),
            slot=slot,
            snapshot=snapshot,
            execution_availability=[],
            now=100,
        )
        self.assertIn("ACTION_VERB_UNSUPPORTED", offer.reason_codes)
        self.assertNotIn("EXECUTION_CHANNEL_UNAVAILABLE", offer.reason_codes)
        self.assertEqual(70, len(deterministic_event_id("stable-key")))


class PureLoopAndActionTest(unittest.TestCase):
    def test_assignment_produces_one_submit_effect(self):
        slot = make_slot("search", "search")
        assignment = {
            "assignment_id": "task-1:search",
            "slot_id": "search",
            "device_id": "dog-a",
            "supporting_bid_event_id": "bid-1",
            "assignment_epoch": 1,
            "allowed_actions": ["move_to"],
            "action_template": slot.action_template,
            "completion_rule": slot.completion_rule,
        }
        event = make_blackboard_event(
            CoordinationEventType.TASK_ASSIGNED,
            Ledger.TASK,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 1,
                "plan_id": "plan:task-1:1:1",
                "assignments": [assignment],
            },
            "coordinator",
            "assignment:task-1:1:1",
        )
        loop_input = LoopInput(
            event=event,
            offset=0,
            now=100,
            session=TaskSession.observed("task-1"),
            task_view={
                "task_revision": 1,
                "coordination_epoch": 1,
                "terminal_event_id": None,
                "replan_pending": False,
                "current_plan": {
                    "plan_id": "plan:task-1:1:1",
                    "assignments": [assignment],
                },
            },
            agent_snapshot=make_snapshot("dog-a", "search"),
            evidence_view=None,
            execution_availability=[],
            skill_references=[],
            local_proposal=None,
        )
        result = PureAgentLoop("dog-a").step(loop_input)
        self.assertTrue(result.consume_input)
        self.assertEqual("submit_action_intent", result.effects[0].kind.value)
        self.assertEqual("task-1:search", result.session_after.assignment.assignment_id)

    def test_stale_assignment_epoch_never_dispatches(self):
        slot = make_slot("search", "search")
        old_assignment = {
            "assignment_id": "task-1:search",
            "slot_id": "search",
            "device_id": "dog-a",
            "supporting_bid_event_id": "bid-old",
            "assignment_epoch": 1,
            "allowed_actions": ["move_to"],
            "action_template": slot.action_template,
            "completion_rule": slot.completion_rule,
        }
        current_assignment = dict(old_assignment)
        current_assignment["supporting_bid_event_id"] = "bid-new"
        current_assignment["assignment_epoch"] = 2
        event = make_blackboard_event(
            CoordinationEventType.TASK_ASSIGNED,
            Ledger.TASK,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 1,
                "plan_id": "plan:task-1:1:1",
                "assignments": [old_assignment],
            },
            "coordinator",
            "assignment:task-1:1:1",
        )
        loop_input = LoopInput(
            event=event,
            offset=10,
            now=100,
            session=TaskSession.observed("task-1"),
            task_view={
                "task_revision": 1,
                "coordination_epoch": 2,
                "terminal_event_id": None,
                "replan_pending": False,
                "current_plan": {
                    "plan_id": "plan:task-1:1:2",
                    "assignments": [current_assignment],
                },
            },
            agent_snapshot=make_snapshot("dog-a", "search"),
            evidence_view=None,
            execution_availability=[],
            skill_references=[],
            local_proposal=None,
        )
        result = PureAgentLoop("dog-a").step(loop_input)
        self.assertTrue(result.consume_input)
        self.assertEqual([], result.effects)
        self.assertIsNone(result.session_after.current_intent)

    def test_r0_dispatch_and_r2_block(self):
        assignment = type(
            "Assignment",
            (),
            {
                "assignment_id": "task-1:search",
                "assignment_epoch": 1,
                "device_id": "dog-a",
                "allowed_actions": ["move_to"],
                "action_template": {
                    "verb": "move_to",
                    "params": {"target": "A"},
                    "reversible": True,
                },
            },
        )()
        payload = DeterministicActionPolicy().build_submit_payload(
            task_id="task-1",
            task_revision=1,
            coordination_epoch=1,
            plan_id="plan:task-1:1:1",
            assignment=assignment,
        )

        published = []

        def publish(event):
            published.append(event)
            return AppendResult(
                AppendStatus.APPENDED, len(published) - 1, len(published), event.id
            )

        gateway = MockPhysicalActionGateway()
        result = ActionExecutor(
            safety=StaticSafetyPort(), gateway=gateway, publish_event=publish
        ).execute(payload)
        self.assertEqual(1, len(gateway.dispatched_intent_ids))
        self.assertEqual("receipt", event_type_value(result.receipt_event))

        blocked_gateway = MockPhysicalActionGateway()
        blocked = ActionExecutor(
            safety=StaticSafetyPort(
                pre=SafetyVerdict(False, RiskLevel.R2, False, "GEOFENCE")
            ),
            gateway=blocked_gateway,
            publish_event=publish,
        ).execute(payload)
        self.assertEqual([], blocked_gateway.dispatched_intent_ids)
        self.assertEqual("safety_intercept", event_type_value(blocked.safety_intercept_event))

    def test_dispatch_exception_becomes_unknown_receipt_without_retry(self):
        assignment = type(
            "Assignment",
            (),
            {
                "assignment_id": "task-1:search",
                "assignment_epoch": 1,
                "device_id": "dog-a",
                "allowed_actions": ["move_to"],
                "action_template": {
                    "verb": "move_to",
                    "params": {"target": "A"},
                    "reversible": True,
                },
            },
        )()
        payload = DeterministicActionPolicy().build_submit_payload(
            task_id="task-1",
            task_revision=1,
            coordination_epoch=1,
            plan_id="plan:task-1:1:1",
            assignment=assignment,
        )

        class UnknownGateway:
            def __init__(self):
                self.calls = 0

            def is_available(self, device_id, verb):
                return True

            def dispatch(self, intent):
                self.calls += 1
                raise TimeoutError("unknown delivery")

        published = []

        def publish(event):
            published.append(event)
            return AppendResult(AppendStatus.APPENDED, 0, len(published), event.id)

        gateway = UnknownGateway()
        result = ActionExecutor(
            safety=StaticSafetyPort(), gateway=gateway, publish_event=publish
        ).execute(payload)
        self.assertEqual(1, gateway.calls)
        self.assertEqual("unknown", result.receipt_event.content["outcome_certainty"])
        self.assertEqual("DISPATCH_UNKNOWN", result.receipt_event.content["failure_reason"])


class PersistenceAndCoordinatorTest(unittest.TestCase):
    def test_file_session_and_outbox(self):
        with tempfile.TemporaryDirectory() as root:
            store = FileTaskSessionStore(root, "dog-a")
            session = TaskSession.observed("task-1")
            store.save_session(session)
            self.assertEqual("task-1", store.load_session("task-1").task_id)
            event = make_blackboard_event(
                CoordinationEventType.BID,
                Ledger.TASK,
                {"task_id": "task-1"},
                "dog-a",
                "bid:task-1:1:1:1:dog-a",
            )
            store.stage_task_event("task-1", event)
            self.assertEqual(1, len(store.list_task_events("task-1")))
            store.resolve_task_event("task-1", event.idempotency_key)
            self.assertEqual([], store.list_task_events("task-1"))

    def test_outbox_reuses_first_canonical_event_until_commit(self):
        with tempfile.TemporaryDirectory() as root:
            store = FileCoordinatorStore(root)
            board = RecordingBlackboard()
            publisher = DurableEventPublisher(
                board,
                lambda event: store.stage_event(event, source_offset=7),
                store.resolve_event,
                store.reject_event,
                auto_resolve_success=False,
            )
            first = make_blackboard_event(
                CoordinationEventType.BID_ROUND_OPENED,
                Ledger.TASK,
                {"task_id": "task-1", "deadline": 110.0},
                "coordinator",
                "bid-round:task-1:1:1:1",
            )
            publisher.publish(first)
            self.assertEqual(1, len(store.list_events()))
            regenerated = make_blackboard_event(
                CoordinationEventType.BID_ROUND_OPENED,
                Ledger.TASK,
                {"task_id": "task-1", "deadline": 999.0},
                "coordinator",
                "bid-round:task-1:1:1:1",
            )
            publisher.publish(regenerated)
            self.assertEqual(110.0, board.events[-1].content["deadline"])
            store.save_consumer_offset(7)
            publisher.resolve(first.idempotency_key)
            self.assertEqual([], store.list_events())

    def test_rejected_output_is_quarantined(self):
        class RejectingBlackboard(RecordingBlackboard):
            def append(self, event):
                return AppendResult(
                    AppendStatus.REJECTED,
                    None,
                    None,
                    event.id,
                    reason_code="SCHEMA_INVALID",
                )

        with tempfile.TemporaryDirectory() as root:
            store = FileCoordinatorStore(root)
            publisher = DurableEventPublisher(
                RejectingBlackboard(),
                store.stage_event,
                store.resolve_event,
                store.reject_event,
                auto_resolve_success=False,
            )
            event = make_blackboard_event(
                CoordinationEventType.BID,
                Ledger.TASK,
                {"task_id": "task-1"},
                "dog-a",
                "bid:task-1:1:1:1:dog-a",
            )
            with self.assertRaises(PermanentAppendError):
                publisher.publish(event)
            self.assertEqual([], store.list_events())
            self.assertEqual(1, len(list((Path(store.root) / "rejected").glob("*.json"))))
            with self.assertRaisesRegex(ValueError, "PREVIOUSLY_REJECTED"):
                publisher.publish(event)

    def test_orphan_task_outbox_is_scanned_and_commit_aware(self):
        with tempfile.TemporaryDirectory() as root:
            store = FileTaskSessionStore(root, "dog-a")
            event = make_blackboard_event(
                CoordinationEventType.BID,
                Ledger.TASK,
                {"task_id": "task-1"},
                "dog-a",
                "bid:task-1:1:1:1:dog-a",
            )
            store.stage_task_event("task-1", event, source_offset=5)
            self.assertEqual([], store.list_sessions())
            board = RecordingBlackboard()
            gateway = MockPhysicalActionGateway()
            host = AgentProcessHost(
                spec=AgentProcessSpec("dog-a", "bb", root),
                blackboard=board,
                loop=PureAgentLoop("dog-a"),
                session_store=store,
                action_executor=ActionExecutor(
                    safety=StaticSafetyPort(), gateway=gateway
                ),
                action_gateway=gateway,
                skill_provider=NullSkillReferenceProvider(),
            )
            store.save_consumer_offset(4)
            self.assertEqual(0, host.replay_outbox())
            self.assertEqual(1, len(store.list_all_task_event_records()))
            store.save_consumer_offset(5)
            self.assertEqual(1, host.replay_outbox())
            self.assertEqual([], store.list_all_task_event_records())

    def test_same_session_version_cannot_change_content(self):
        with tempfile.TemporaryDirectory() as root:
            store = FileTaskSessionStore(root, "dog-a")
            session = TaskSession.observed("task-1")
            store.save_session(session)
            store.save_session(session)
            changed = TaskSession.observed("task-1")
            changed.coordination_epoch = 2
            with self.assertRaisesRegex(ValueError, "same session version"):
                store.save_session(changed)

    def test_coordinator_opens_round(self):
        task = make_task([make_slot("search", "search")])
        board = RecordingBlackboard()
        board.views["task_coordination"] = {
            "task_id": "task-1",
            "task_revision": 1,
            "task": task,
            "status": "posted",
            "coordination_epoch": 0,
            "terminal_event_id": None,
            "view_version": 1,
        }
        with tempfile.TemporaryDirectory() as root:
            coordinator = Coordinator(
                blackboard=board,
                store=FileCoordinatorStore(root),
                bid_window_s=10,
            )
            posted = make_blackboard_event(
                CoordinationEventType.TASK_POSTED,
                Ledger.TASK,
                task,
                "ingress",
                "task-posted:task-1:1",
            )
            posted.version = 1
            outputs = coordinator.handle(EventEnvelope(0, posted))
            self.assertEqual(1, len(outputs))
            self.assertEqual("bid_round_opened", event_type_value(outputs[0]))
            self.assertEqual(1, len(board.events))

    def test_coordinator_closes_round_with_complete_plan(self):
        slot = make_slot("search", "search")
        task = make_task([slot])
        snapshot = make_snapshot("dog-a", "search")
        bid = EligibilityBidEngine().make_bid(
            task=task,
            role_slots=[slot],
            snapshot=snapshot,
            execution_availability=[ExecutionAvailability("dog-a", "move_to", True, 100)],
            task_revision=1,
            coordination_epoch=1,
            bid_round=1,
            deadline=110,
            now=100,
            proposal=None,
        )
        board = RecordingBlackboard()
        board.clock = 111
        board.views["task_coordination"] = {
            "task_id": "task-1",
            "task_revision": 1,
            "task": task,
            "status": "bidding",
            "coordination_epoch": 1,
            "terminal_event_id": None,
            "view_version": 1,
        }
        board.views["bid_round"] = {
            "bids_by_device": {
                "dog-a": {
                    "event_id": "bid-1",
                    "event_version": 1,
                    "event_ts": 100,
                    "payload": to_json_value(bid),
                }
            }
        }
        board.views["agent_public"] = {"dog-a": to_json_value(snapshot)}
        board.views["evidence"] = {}
        with tempfile.TemporaryDirectory() as root:
            coordinator = Coordinator(
                blackboard=board, store=FileCoordinatorStore(root)
            )
            output = coordinator.close_bid_round(
                ActiveBidRound("task-1", 1, 1, 1, 110)
            )
            self.assertIsNotNone(output)
            self.assertEqual("task_assigned", event_type_value(output))
            self.assertEqual("dog-a", output.content["assignments"][0]["device_id"])

    def test_group_policy_error_falls_back_but_validator_error_surfaces(self):
        class FailingPolicy:
            def plan(self, planning_input, timeout_s):
                raise TimeoutError("model timeout")

        board, round_state = make_single_slot_planning_board()
        with tempfile.TemporaryDirectory() as root:
            coordinator = Coordinator(
                blackboard=board,
                store=FileCoordinatorStore(root),
                group_policy=FailingPolicy(),
            )
            output = coordinator.close_bid_round(round_state)
            self.assertEqual("task_assigned", event_type_value(output))

        class ValidPolicy:
            def plan(self, planning_input, timeout_s):
                return deterministic_maximum_matching(planning_input)

        class BrokenValidator:
            def validate(self, candidate, planning_input):
                raise RuntimeError("validator bug")

        board, round_state = make_single_slot_planning_board()
        with tempfile.TemporaryDirectory() as root:
            coordinator = Coordinator(
                blackboard=board,
                store=FileCoordinatorStore(root),
                group_policy=ValidPolicy(),
                validator=BrokenValidator(),
            )
            with self.assertRaisesRegex(RuntimeError, "validator bug"):
                coordinator.close_bid_round(round_state)

    def test_group_candidate_is_discarded_when_authoritative_input_changes(self):
        slot = make_slot("search", "search")
        task = make_task([slot])
        snapshots = [make_snapshot("dog-a", "search"), make_snapshot("dog-b", "search")]
        bids = []
        for index, snapshot in enumerate(snapshots, start=1):
            payload = EligibilityBidEngine().make_bid(
                task=task,
                role_slots=[slot],
                snapshot=snapshot,
                execution_availability=[
                    ExecutionAvailability(snapshot.device_id, "move_to", True, 100)
                ],
                task_revision=1,
                coordination_epoch=1,
                bid_round=1,
                deadline=110,
                now=100,
                proposal=None,
            )
            bids.append((snapshot.device_id, index, payload))
        board = RecordingBlackboard()
        board.clock = 111
        board.views["task_coordination"] = {
            "task_id": "task-1",
            "task_revision": 1,
            "task": task,
            "status": "bidding",
            "coordination_epoch": 1,
            "terminal_event_id": None,
            "replan_pending": False,
            "view_version": 1,
        }
        board.views["bid_round"] = {
            "bids_by_device": {
                device_id: {
                    "event_id": f"bid-{index}",
                    "event_version": index,
                    "event_ts": 100,
                    "payload": to_json_value(payload),
                }
                for device_id, index, payload in bids
            }
        }
        board.views["agent_public"] = {
            snapshot.device_id: to_json_value(snapshot) for snapshot in snapshots
        }
        board.views["evidence"] = {}

        class MutatingPolicy:
            def plan(self, planning_input, timeout_s):
                board.views["evidence"] = {
                    "clues_by_key": {
                        "new": {"event_id": "clue-new", "event_version": 1}
                    }
                }
                marker = make_blackboard_event(
                    CoordinationEventType.CLUE,
                    Ledger.EVIDENCE,
                    {"task_id": "other-task"},
                    "camera",
                    "clue:other-task:camera:new",
                )
                marker.version = 1
                board.events.append(marker)
                return CandidateAssignmentPlan(
                    task_id="task-1",
                    task_revision=1,
                    coordination_epoch=1,
                    assignments=[CandidateAssignment("search", "dog-b", "bid-2")],
                    rationale_summary="model prefers dog-b",
                    input_fingerprint=planning_input.input_fingerprint,
                )

        with tempfile.TemporaryDirectory() as root:
            coordinator = Coordinator(
                blackboard=board,
                store=FileCoordinatorStore(root),
                group_policy=MutatingPolicy(),
            )
            output = coordinator.close_bid_round(
                ActiveBidRound("task-1", 1, 1, 1, 110)
            )
            self.assertEqual("dog-a", output.content["assignments"][0]["device_id"])

    def test_coordinator_recovers_active_round_from_blackboard_history(self):
        opened = make_blackboard_event(
            CoordinationEventType.BID_ROUND_OPENED,
            Ledger.TASK,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 1,
                "bid_round": 1,
                "slots": ["search"],
                "deadline": 110.0,
            },
            "coordinator",
            "bid-round:task-1:1:1:1",
        )
        opened.version = 1
        board = HistoryBlackboard([EventEnvelope(0, opened)])
        with tempfile.TemporaryDirectory() as root:
            coordinator = Coordinator(
                blackboard=board, store=FileCoordinatorStore(root)
            )
            self.assertEqual(1, coordinator.recover_control_state())
            self.assertIn("task-1", coordinator._active_rounds)

        assigned = make_blackboard_event(
            CoordinationEventType.TASK_ASSIGNED,
            Ledger.TASK,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 1,
                "plan_id": "plan:task-1:1:1",
                "assignments": [],
            },
            "coordinator",
            "assignment:task-1:1:1",
        )
        assigned.version = 2
        board = HistoryBlackboard(
            [EventEnvelope(0, opened), EventEnvelope(1, assigned)]
        )
        with tempfile.TemporaryDirectory() as root:
            coordinator = Coordinator(
                blackboard=board, store=FileCoordinatorStore(root)
            )
            coordinator.recover_control_state()
            self.assertNotIn("task-1", coordinator._active_rounds)

    def test_receipt_converges_to_assignment_and_task_done(self):
        slot = make_slot("search", "search")
        task = make_task([slot])
        assignment = {
            "assignment_id": "task-1:search",
            "slot_id": "search",
            "device_id": "dog-a",
            "supporting_bid_event_id": "bid-1",
            "assignment_epoch": 1,
            "allowed_actions": ["move_to"],
            "action_template": slot.action_template,
            "completion_rule": slot.completion_rule,
        }
        plan = {
            "task_id": "task-1",
            "task_revision": 1,
            "coordination_epoch": 1,
            "plan_id": "plan:task-1:1:1",
            "assignments": [assignment],
        }
        board = RecordingBlackboard()

        def task_view(_):
            completed = any(
                event_type_value(event) == "assignment_completed" for event in board.events
            )
            return {
                "task_id": "task-1",
                "task_revision": 1,
                "task": task,
                "status": "assigned",
                "coordination_epoch": 1,
                "current_plan": plan,
                "completed_assignment_ids": ["task-1:search"] if completed else [],
                "terminal_event_id": None,
                "replan_pending": False,
                "view_version": len(board.events),
            }

        board.views["task_coordination"] = task_view
        with tempfile.TemporaryDirectory() as root:
            coordinator = Coordinator(
                blackboard=board, store=FileCoordinatorStore(root)
            )
            receipt = make_blackboard_event(
                CoordinationEventType.RECEIPT,
                Ledger.RECEIPT,
                {
                    "schema_version": 2,
                    "receipt_id": "receipt:intent-1",
                    "task_id": "task-1",
                    "task_revision": 1,
                    "coordination_epoch": 1,
                    "plan_id": "plan:task-1:1:1",
                    "assignment_id": "task-1:search",
                    "assignment_epoch": 1,
                    "intent_id": "intent-1",
                    "device_id": "dog-a",
                    "attempt": 1,
                    "success": True,
                    "outcome_certainty": "confirmed",
                    "result": {},
                    "post_check_allowed": True,
                },
                "dog-a",
                "receipt:intent-1",
            )
            receipt.version = 1
            outputs = coordinator.handle(EventEnvelope(0, receipt))
            self.assertEqual(
                ["assignment_completed", "task_done"],
                [event_type_value(event) for event in outputs],
            )
            self.assertEqual(
                ["assignment_completed", "task_done"],
                [event_type_value(event) for event in board.events],
            )

    def test_clue_replans_and_estop_fails_explicitly(self):
        slot = make_slot("search", "search")
        task = make_task([slot])
        plan = {
            "plan_id": "plan:task-1:1:1",
            "coordination_epoch": 1,
            "assignments": [],
            "input_evidence_refs": [],
        }
        clue = make_blackboard_event(
            CoordinationEventType.CLUE,
            Ledger.EVIDENCE,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "clue_id": "clue-1",
            },
            "camera-a",
            "clue:task-1:camera-a:clue-1",
        )
        clue.version = 5
        board = RecordingBlackboard()
        board.views["task_coordination"] = {
            "task_id": "task-1",
            "task_revision": 1,
            "task": task,
            "status": "assigned",
            "coordination_epoch": 1,
            "current_plan": plan,
            "terminal_event_id": None,
            "replan_pending": True,
            "pending_replan_cause_ids": [clue.id],
            "view_version": 5,
        }
        board.views["action"] = {
            "intents_by_id": {},
            "receipts_by_intent": {},
            "intercepts_by_intent": {},
        }
        board.views["evidence"] = {
            "clues_by_key": {
                "clue-1": {"event_id": clue.id, "event_version": 5}
            }
        }
        with tempfile.TemporaryDirectory() as root:
            coordinator = Coordinator(
                blackboard=board, store=FileCoordinatorStore(root)
            )
            outputs = coordinator.handle(EventEnvelope(4, clue))
            self.assertEqual(
                ["task_replan", "bid_round_opened"],
                [event_type_value(event) for event in outputs],
            )

        estop = make_blackboard_event(
            CoordinationEventType.ESTOP,
            Ledger.RECEIPT,
            {
                "schema_version": 2,
                "signal_id": "stop-1",
                "operation": "stop",
                "scope_type": "device",
                "scope_id": "dog-a",
                "task_id": "task-1",
                "task_revision": 1,
            },
            "safety",
            "estop:stop-1:stop",
        )
        estop.version = 6
        board.events.clear()
        board.views["task_coordination"]["replan_pending"] = False
        with tempfile.TemporaryDirectory() as root:
            coordinator = Coordinator(
                blackboard=board, store=FileCoordinatorStore(root)
            )
            outputs = coordinator.handle(EventEnvelope(5, estop))
            self.assertEqual(["task_failed"], [event_type_value(event) for event in outputs])
            self.assertEqual("ESTOP_TRIGGERED", outputs[0].content["reason_code"])

    def test_agent_host_replay_does_not_dispatch_twice(self):
        slot = make_slot("search", "search")
        task = make_task([slot])
        snapshot = make_snapshot("dog-a", "search")
        assignment = {
            "assignment_id": "task-1:search",
            "slot_id": "search",
            "device_id": "dog-a",
            "supporting_bid_event_id": "bid-1",
            "assignment_epoch": 1,
            "allowed_actions": ["move_to"],
            "action_template": slot.action_template,
            "completion_rule": slot.completion_rule,
        }
        event = make_blackboard_event(
            CoordinationEventType.TASK_ASSIGNED,
            Ledger.TASK,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 1,
                "plan_id": "plan:task-1:1:1",
                "assignments": [assignment],
            },
            "coordinator",
            "assignment:task-1:1:1",
        )
        event.version = 1
        board = RecordingBlackboard()
        board.views["task_coordination"] = {
            "task_id": "task-1",
            "task_revision": 1,
            "task": task,
            "status": "assigned",
            "coordination_epoch": 1,
            "current_plan": {
                "plan_id": "plan:task-1:1:1",
                "assignments": [assignment],
            },
            "terminal_event_id": None,
            "replan_pending": False,
            "view_version": 1,
        }
        board.views["agent_public"] = to_json_value(snapshot)
        board.views["evidence"] = {}
        gateway = MockPhysicalActionGateway()
        with tempfile.TemporaryDirectory() as root:
            store = FileTaskSessionStore(root, "dog-a")
            host = AgentProcessHost(
                spec=AgentProcessSpec("dog-a", "bb", root),
                blackboard=board,
                loop=PureAgentLoop("dog-a"),
                session_store=store,
                action_executor=ActionExecutor(
                    safety=StaticSafetyPort(), gateway=gateway
                ),
                action_gateway=gateway,
                skill_provider=NullSkillReferenceProvider(),
            )
            self.assertTrue(host._handle_envelope(EventEnvelope(0, event)))
            self.assertEqual(1, len(gateway.dispatched_intent_ids))
            self.assertEqual("dispatching", store.load_session("task-1").current_intent.state.value)
            self.assertTrue(host._handle_envelope(EventEnvelope(1, event)))
            self.assertEqual(1, len(gateway.dispatched_intent_ids))


class BlackboardClientContractTest(unittest.TestCase):
    def test_stale_view_is_not_returned(self):
        board = RecordingBlackboard()
        board.views["task_coordination"] = {"view_version": 1}
        client = BlackboardClient(board)
        with self.assertRaises(ViewNotCaughtUpError):
            client.query_view(
                "task",
                {"view_type": "task_coordination"},
                min_version=2,
                timeout_s=1,
            )

    def test_non_monotonic_offsets_are_rejected(self):
        board = RecordingBlackboard()
        event = make_blackboard_event(
            CoordinationEventType.TASK_POSTED,
            Ledger.TASK,
            {"task_id": "task-1"},
            "ingress",
            "task-posted:task-1:1",
        )
        board.read_since = lambda *args: [EventEnvelope(0, event), EventEnvelope(0, event)]
        with self.assertRaises(BlackboardContractError):
            BlackboardClient(board).read_since(-1, {"task_posted"}, 10, 1)


if __name__ == "__main__":
    unittest.main()
