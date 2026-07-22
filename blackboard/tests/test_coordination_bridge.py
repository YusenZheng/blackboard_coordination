from __future__ import annotations

import copy
import tempfile
import unittest

from swarm_brain.blackboard.board import Blackboard
from swarm_brain.blackboard.view import fold_view
from swarm_brain.access.registry import Registry
from swarm_brain.contracts.agent_card import AgentCard, CapabilitySlot
from swarm_brain.contracts.blackboard_event import (
    AppendStatus,
    BlackboardEvent,
    EventType,
    Ledger,
)
from swarm_brain.contracts.types import (
    CapabilityProfile,
    DeviceRef,
    DeviceState,
    DeviceType,
)
from swarm_brain.contracts.verbs import ActionVerb
from swarm_brain.coordination.action_executor import ActionExecutor
from swarm_brain.coordination.adapters import (
    MockPhysicalActionGateway,
    NullSkillReferenceProvider,
    StaticSafetyPort,
)
from swarm_brain.coordination.agent_loop import PureAgentLoop
from swarm_brain.coordination.agent_process import AgentProcessHost
from swarm_brain.coordination.blackboard_client import BlackboardClient
from swarm_brain.coordination.coordinator import Coordinator
from swarm_brain.coordination.file_session_store import (
    FileCoordinatorStore,
    FileTaskSessionStore,
)
from swarm_brain.coordination.models import (
    AgentProcessSpec,
    CoordinationEventType,
    event_type_value,
    make_blackboard_event,
)


class ManualClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def task_payload(task_id: str = "task-1", revision: int = 1) -> dict:
    return {
        "schema_version": 2,
        "task_id": task_id,
        "task_revision": revision,
        "task_type": "search",
        "goal": "search target",
        "requirement": {
            "required_capabilities": ["search"],
            "min_battery": 0.2,
            "min_endurance_s": 60,
            "space_constraints": [],
        },
        "role_slots": [
            {
                "slot_id": "search",
                "required_capability_ids": ["search"],
                "exclusive": True,
                "allowed_actions": ["move_to"],
                "action_template": {
                    "verb": "move_to",
                    "params": {"target": "zone-a"},
                    "reversible": True,
                },
                "completion_rule": {
                    "kind": "receipt_success",
                    "required_result_fields": [],
                },
            }
        ],
    }


def public_snapshot() -> dict:
    return {
        "device_id": "dog-a",
        "device_type": "dog",
        "online": True,
        "healthy": True,
        "busy": False,
        "busy_task_id": None,
        "battery": 0.9,
        "endurance_s": 600,
        "position": None,
        "current_action": "idle",
        "capabilities": ["search"],
        "action_verbs": ["move_to"],
        "constraints": [],
        "width_cm": 40,
        "lowest_posture_cm": None,
        "step_limit_cm": None,
        "payload_kg": None,
        "success_rate": 0.95,
        "card_version": 1,
        "state_updated_at": 100.0,
    }


def assignment_payload(
    *, task_id: str = "task-1", epoch: int = 1, device_id: str = "dog-a"
) -> dict:
    return {
        "assignment_id": f"{task_id}:search",
        "slot_id": "search",
        "device_id": device_id,
        "supporting_bid_event_id": f"bid:{task_id}:{epoch}:{device_id}",
        "assignment_epoch": epoch,
        "allowed_actions": ["move_to"],
        "action_template": {
            "verb": "move_to",
            "params": {"target": "zone-a"},
            "reversible": True,
        },
        "completion_rule": {
            "kind": "receipt_success",
            "required_result_fields": [],
        },
    }


def plan_payload(
    *, task_id: str = "task-1", revision: int = 1, epoch: int = 1
) -> dict:
    return {
        "schema_version": 2,
        "task_id": task_id,
        "task_revision": revision,
        "coordination_epoch": epoch,
        "plan_id": f"plan:{task_id}:{revision}:{epoch}",
        "assignments": [assignment_payload(task_id=task_id, epoch=epoch)],
        "committed_by": "coordinator",
        "input_evidence_refs": [],
        "input_fingerprint": f"fingerprint:{task_id}:{revision}:{epoch}",
    }


def append_event(
    board: Blackboard,
    event_type: EventType,
    ledger: Ledger,
    content: dict,
    key: str,
    *,
    event_id: str | None = None,
) -> BlackboardEvent:
    event = BlackboardEvent(
        id=event_id or key,
        type=event_type,
        ledger=ledger,
        content=copy.deepcopy(content),
        source="test",
        idempotency_key=key,
    )
    result = board.append(event)
    if result.status != AppendStatus.APPENDED:
        raise AssertionError(result)
    return event


class BlackboardAuthorityTest(unittest.TestCase):
    def test_append_cursor_watermark_and_empty_view_checkpoint(self) -> None:
        clock = ManualClock()
        board = Blackboard(clock=clock)
        posted = append_event(
            board,
            EventType.TASK_POSTED,
            Ledger.TASK,
            task_payload(),
            "task-posted:task-1:1",
        )
        self.assertEqual(1, posted.version)
        self.assertEqual(100.0, posted.ts)
        self.assertEqual(1, board.high_watermark())
        first = board.read_since(-1, {"task_posted"}, 10, 0)
        self.assertEqual([0], [item.offset for item in first])
        self.assertEqual([], board.read_since(0, {"task_posted"}, 10, 0))

        client = BlackboardClient(board)
        empty = client.query_view(
            Ledger.RECEIPT.value,
            {
                "view_type": "action",
                "task_id": "task-1",
                "task_revision": 1,
            },
            min_version=1,
            timeout_s=0,
        )
        self.assertEqual(1, empty["view_version"])
        self.assertEqual({}, empty["intents_by_id"])

    def test_idempotency_key_and_terminal_have_one_canonical_winner(self) -> None:
        board = Blackboard(clock=ManualClock())
        posted = BlackboardEvent(
            id="posted-1",
            type=EventType.TASK_POSTED,
            ledger=Ledger.TASK,
            content=task_payload(),
            source="ingress",
            idempotency_key="task-posted:task-1:1",
        )
        first = board.append(posted)
        duplicate = board.append(copy.deepcopy(posted))
        self.assertEqual(AppendStatus.APPENDED, first.status)
        self.assertEqual(AppendStatus.DUPLICATE_SAME, duplicate.status)
        self.assertEqual((0, 1), (duplicate.offset, duplicate.version))

        drifted = copy.deepcopy(posted)
        drifted.id = "posted-drifted"
        drifted.content["goal"] = "different"
        conflict = board.append(drifted)
        self.assertEqual(AppendStatus.KEY_CONFLICT, conflict.status)
        self.assertEqual(1, board.high_watermark())

        failed = append_event(
            board,
            EventType.TASK_FAILED,
            Ledger.TASK,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 0,
                "reason_code": "NO_FEASIBLE_TEAM",
                "failed_component": "coordinator",
                "related_event_ids": [],
                "retryable": False,
                "failed_at": 100.0,
            },
            "task-terminal:task-1:1",
            event_id="failed-1",
        )
        done = BlackboardEvent(
            id="done-1",
            type=EventType.TASK_DONE,
            ledger=Ledger.TASK,
            content={
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 1,
                "plan_id": "plan:task-1:1:1",
                "completed_assignment_ids": ["task-1:search"],
                "completion_evidence": [],
                "completed_at": 100.0,
            },
            source="coordinator",
            # Different key must not bypass the task-level terminal invariant.
            idempotency_key="different-terminal-key",
        )
        terminal_conflict = board.append(done)
        self.assertEqual(AppendStatus.TERMINAL_CONFLICT, terminal_conflict.status)
        self.assertEqual(failed.version, terminal_conflict.version)
        self.assertEqual(failed.id, terminal_conflict.existing_event_id)
        self.assertEqual(2, board.high_watermark())

    def test_malformed_v2_event_is_rejected_without_polluting_indexes(self) -> None:
        board = Blackboard(clock=ManualClock())
        append_event(
            board,
            EventType.TASK_POSTED,
            Ledger.TASK,
            task_payload(),
            "task-posted:task-1:1",
        )
        delivered: list[int] = []
        board.subscribe(
            lambda _event, offset: delivered.append(offset), from_offset=1
        )
        malformed = BlackboardEvent(
            id="round-with-bad-epoch",
            type=EventType.BID_ROUND_OPENED,
            ledger=Ledger.TASK,
            content={
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": "bad",
                "bid_round": 1,
                "slots": ["search"],
                "deadline": 110.0,
            },
            source="coordinator",
            idempotency_key="round-with-bad-epoch",
        )

        rejected = board.append(malformed)
        self.assertEqual(AppendStatus.REJECTED, rejected.status)
        self.assertIsNone(rejected.offset)
        self.assertIsNone(rejected.version)
        self.assertEqual("COORDINATION_EPOCH_TYPE_INVALID", rejected.reason_code)
        self.assertEqual(1, board.high_watermark())
        self.assertEqual([], delivered)
        self.assertEqual([], board.read_since(0, None, 10, 0))

        # A rejected write must not reserve either its event id or idempotency key.
        malformed.content["coordination_epoch"] = 1
        repaired = board.append(malformed)
        self.assertEqual(AppendStatus.APPENDED, repaired.status)
        self.assertEqual((1, 2), (repaired.offset, repaired.version))
        self.assertEqual([1], delivered)

        downgrade = BlackboardEvent(
            id="legacy-terminal-on-v2-task",
            type=EventType.TASK_DONE,
            ledger=Ledger.TASK,
            content={"task_id": "task-1"},
            source="legacy-writer",
        )
        downgrade_result = board.append(downgrade)
        self.assertEqual(AppendStatus.REJECTED, downgrade_result.status)
        self.assertEqual("SCHEMA_VERSION_REQUIRED", downgrade_result.reason_code)
        self.assertEqual(2, board.high_watermark())

    def test_v2_schema_guards_reject_unparseable_payloads(self) -> None:
        cases = [
            BlackboardEvent(
                id="bad-schema",
                type=EventType.TASK_POSTED,
                ledger=Ledger.TASK,
                content={
                    "schema_version": "2",
                    "task_id": "task-1",
                    "task_revision": 1,
                    "role_slots": [],
                },
                source="test",
            ),
            BlackboardEvent(
                id="clue-without-revision",
                type=EventType.CLUE,
                ledger=Ledger.EVIDENCE,
                content={
                    "schema_version": 2,
                    "task_id": "task-1",
                    "clue_id": "clue-1",
                },
                source="test",
            ),
            BlackboardEvent(
                id="round-without-schema",
                type=EventType.BID_ROUND_OPENED,
                ledger=Ledger.TASK,
                content={
                    "task_id": "task-1",
                    "task_revision": 1,
                    "coordination_epoch": 1,
                    "bid_round": 1,
                    "slots": [],
                    "deadline": 110.0,
                },
                source="test",
            ),
            BlackboardEvent(
                id="shared-v2-shape-without-schema",
                type=EventType.TASK_ASSIGNED,
                ledger=Ledger.TASK,
                content={
                    "task_id": "task-1",
                    "task_revision": 1,
                    "coordination_epoch": 1,
                    "plan_id": "injected-plan",
                    "assignments": [],
                },
                source="test",
            ),
            BlackboardEvent(
                id="unsafe-legacy-structure",
                type=EventType.TASK_ASSIGNED,
                ledger=Ledger.TASK,
                content={"task_id": "task-1", "assignments": ["bad"]},
                source="test",
            ),
            BlackboardEvent(
                id="replan-skips-epoch",
                type=EventType.TASK_REPLAN,
                ledger=Ledger.TASK,
                content={
                    "schema_version": 2,
                    "task_id": "task-1",
                    "task_revision": 1,
                    "cause_event_id": "clue-1",
                    "from_epoch": 1,
                    "to_epoch": 3,
                    "evidence_refs": ["clue-1"],
                },
                source="test",
            ),
        ]
        board = Blackboard(clock=ManualClock())
        for event in cases:
            with self.subTest(event_id=event.id):
                result = board.append(event)
                self.assertEqual(AppendStatus.REJECTED, result.status)
                self.assertIsNotNone(result.reason_code)
        self.assertEqual(0, board.high_watermark())

    def test_non_task_v2_facts_and_late_bids_remain_auditable(self) -> None:
        clock = ManualClock()
        board = Blackboard(clock=clock)
        fusion = BlackboardEvent(
            id="fusion-1",
            type=EventType.FUSION,
            ledger=Ledger.EVIDENCE,
            content={"schema_version": 2, "fusion_id": "fusion-1"},
            source="fusion-service",
        )
        self.assertEqual(AppendStatus.APPENDED, board.append(fusion).status)

        append_event(
            board,
            EventType.TASK_POSTED,
            Ledger.TASK,
            task_payload(),
            "task-posted:task-1:1",
        )
        append_event(
            board,
            EventType.BID_ROUND_OPENED,
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
            "round:task-1:1:1",
        )
        clock.value = 111.0
        late_bid = append_event(
            board,
            EventType.BID,
            Ledger.TASK,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 1,
                "bid_round": 1,
                "device_id": "dog-a",
                "offers": [],
                "proposal": None,
                "expires_at": 110.0,
            },
            "late-bid:task-1:dog-a",
        )
        self.assertEqual(late_bid.id, board.read_events()[-1].id)
        bid_view = board.query_view(
            Ledger.TASK,
            {
                "view_type": "bid_round",
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 1,
                "bid_round": 1,
            },
        )
        self.assertEqual({}, bid_view["bids_by_device"])

    def test_clues_are_bound_to_the_revision_at_commit_time(self) -> None:
        board = Blackboard(clock=ManualClock())
        append_event(
            board,
            EventType.TASK_POSTED,
            Ledger.TASK,
            task_payload(revision=1),
            "task-posted:task-1:1",
        )
        legacy_clue = append_event(
            board,
            EventType.CLUE,
            Ledger.EVIDENCE,
            {"task_id": "task-1", "clue_id": "legacy-r1"},
            "legacy-clue-r1",
        )
        append_event(
            board,
            EventType.TASK_POSTED,
            Ledger.TASK,
            task_payload(revision=2),
            "task-posted:task-1:2",
        )
        current_clue = append_event(
            board,
            EventType.CLUE,
            Ledger.EVIDENCE,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 2,
                "clue_id": "current-r2",
            },
            "current-clue-r2",
        )

        revision_one = board.query_view(
            Ledger.TASK,
            {
                "view_type": "task_coordination",
                "task_id": "task-1",
                "task_revision": 1,
            },
        )
        revision_two = board.query_view(
            Ledger.TASK,
            {
                "view_type": "task_coordination",
                "task_id": "task-1",
                "task_revision": 2,
            },
        )
        self.assertEqual([legacy_clue.id], revision_one["pending_replan_cause_ids"])
        self.assertEqual([current_clue.id], revision_two["pending_replan_cause_ids"])

        evidence_one = board.query_view(
            Ledger.EVIDENCE,
            {
                "view_type": "evidence",
                "task_id": "task-1",
                "task_revision": 1,
            },
        )
        evidence_two = board.query_view(
            Ledger.EVIDENCE,
            {
                "view_type": "evidence",
                "task_id": "task-1",
                "task_revision": 2,
            },
        )
        evidence_default = board.query_view(
            Ledger.EVIDENCE,
            {"view_type": "evidence", "task_id": "task-1"},
        )
        self.assertEqual(["legacy-r1"], list(evidence_one["clues_by_key"]))
        self.assertEqual(["current-r2"], list(evidence_two["clues_by_key"]))
        self.assertEqual(2, evidence_default["task_revision"])
        self.assertEqual(["current-r2"], list(evidence_default["clues_by_key"]))

    def test_new_revision_supersedes_old_assignment_in_agent_public_view(self) -> None:
        board = Blackboard(clock=ManualClock())
        board.upsert_agent_snapshot(public_snapshot())
        append_event(
            board,
            EventType.TASK_POSTED,
            Ledger.TASK,
            task_payload(revision=1),
            "task-posted:task-1:1",
        )
        append_event(
            board,
            EventType.BID_ROUND_OPENED,
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
            "round:task-1:1:1",
        )
        append_event(
            board,
            EventType.TASK_ASSIGNED,
            Ledger.TASK,
            plan_payload(epoch=1),
            "plan:task-1:1:1",
        )
        assigned = board.query_view(
            None, {"view_type": "agent_public", "device_id": "dog-a"}
        )
        self.assertTrue(assigned["busy"])
        self.assertEqual("task-1", assigned["busy_task_id"])

        append_event(
            board,
            EventType.TASK_POSTED,
            Ledger.TASK,
            task_payload(revision=2),
            "task-posted:task-1:2",
        )
        superseded = board.query_view(
            None, {"view_type": "agent_public", "device_id": "dog-a"}
        )
        self.assertFalse(superseded["busy"])
        self.assertIsNone(superseded["busy_task_id"])

    def test_stale_epoch_terminal_is_rejected_before_claiming_terminal_key(self) -> None:
        board = Blackboard(clock=ManualClock())
        append_event(
            board,
            EventType.TASK_POSTED,
            Ledger.TASK,
            task_payload(),
            "task-posted:task-1:1",
        )
        append_event(
            board,
            EventType.BID_ROUND_OPENED,
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
            "round:task-1:1:1",
        )
        append_event(
            board,
            EventType.TASK_ASSIGNED,
            Ledger.TASK,
            plan_payload(epoch=1),
            "plan:task-1:1:1",
        )
        clue = append_event(
            board,
            EventType.CLUE,
            Ledger.EVIDENCE,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "clue_id": "replan-cause",
            },
            "clue:replan-cause",
        )
        append_event(
            board,
            EventType.TASK_REPLAN,
            Ledger.TASK,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "cause_event_id": clue.id,
                "from_epoch": 1,
                "to_epoch": 2,
                "evidence_refs": [clue.id],
            },
            "replan:task-1:1:2",
        )
        append_event(
            board,
            EventType.BID_ROUND_OPENED,
            Ledger.TASK,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 2,
                "bid_round": 1,
                "slots": ["search"],
                "deadline": 110.0,
            },
            "round:task-1:1:2",
        )
        append_event(
            board,
            EventType.TASK_ASSIGNED,
            Ledger.TASK,
            plan_payload(epoch=2),
            "plan:task-1:1:2",
        )

        terminal = BlackboardEvent(
            id="terminal-same-id",
            type=EventType.TASK_FAILED,
            ledger=Ledger.TASK,
            content={
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 1,
                "reason_code": "SAFETY_BLOCKED",
                "failed_component": "safety",
                "related_event_ids": [],
                "retryable": False,
                "failed_at": 100.0,
            },
            source="coordinator",
            idempotency_key="terminal-same-key",
        )

        # Replay may contain history written by an older implementation.  The
        # projector must still isolate stale epoch facts even when append-time
        # admission was not available at the time.
        stale_completion_history = BlackboardEvent(
            id="historic-completion-e1",
            type=EventType.ASSIGNMENT_COMPLETED,
            ledger=Ledger.TASK,
            content={
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 1,
                "plan_id": "plan:task-1:1:1",
                "assignment_id": "task-1:search",
                "assignment_epoch": 1,
            },
            source="historic",
            version=board.high_watermark() + 1,
            ts=100.0,
        )
        stale_terminal_history = copy.deepcopy(terminal)
        stale_terminal_history.version = board.high_watermark() + 2
        stale_terminal_history.ts = 100.0
        replay_history = board.read_events() + [
            stale_completion_history,
            stale_terminal_history,
        ]
        replay_task = fold_view(
            replay_history,
            Ledger.TASK,
            {
                "view_type": "task_coordination",
                "task_id": "task-1",
                "task_revision": 1,
            },
            view_version=len(replay_history),
            now=100.0,
        )
        replay_terminal = fold_view(
            replay_history,
            Ledger.TASK,
            {
                "view_type": "terminal",
                "task_id": "task-1",
                "task_revision": 1,
            },
            view_version=len(replay_history),
            now=100.0,
        )
        self.assertEqual("plan:task-1:1:2", replay_task["current_plan"]["plan_id"])
        self.assertEqual([], replay_task["completed_assignment_ids"])
        self.assertIsNone(replay_task["terminal_event_id"])
        self.assertIsNone(replay_terminal["terminal_event_id"])

        watermark = board.high_watermark()
        stale = board.append(terminal)
        self.assertEqual(AppendStatus.REJECTED, stale.status)
        self.assertEqual("COORDINATION_EPOCH_MISMATCH", stale.reason_code)
        self.assertEqual(watermark, board.high_watermark())

        terminal.content["coordination_epoch"] = 2
        current = board.append(terminal)
        self.assertEqual(AppendStatus.APPENDED, current.status)
        projected = board.query_view(
            Ledger.TASK,
            {
                "view_type": "terminal",
                "task_id": "task-1",
                "task_revision": 1,
            },
        )
        self.assertEqual("terminal-same-id", projected["terminal_event_id"])
        self.assertEqual("failed", projected["status"])

    def test_views_fold_lifecycle_clue_and_stale_epoch(self) -> None:
        board = Blackboard(clock=ManualClock())
        task = task_payload()
        append_event(
            board,
            EventType.TASK_POSTED,
            Ledger.TASK,
            task,
            "task-posted:task-1:1",
        )
        append_event(
            board,
            EventType.BID_ROUND_OPENED,
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
            "bid-round:task-1:1:1:1",
        )
        append_event(
            board,
            EventType.BID,
            Ledger.TASK,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 1,
                "bid_round": 1,
                "device_id": "dog-a",
                "offers": [],
                "proposal": None,
                "expires_at": 110.0,
            },
            "bid:task-1:1:1:1:dog-a",
        )
        assignment = {
            "assignment_id": "task-1:search",
            "slot_id": "search",
            "device_id": "dog-a",
            "supporting_bid_event_id": "bid:task-1:1:1:1:dog-a",
            "assignment_epoch": 1,
            "allowed_actions": ["move_to"],
            "action_template": task["role_slots"][0]["action_template"],
            "completion_rule": task["role_slots"][0]["completion_rule"],
        }
        plan = {
            "schema_version": 2,
            "task_id": "task-1",
            "task_revision": 1,
            "coordination_epoch": 1,
            "plan_id": "plan:task-1:1:1",
            "assignments": [assignment],
            "committed_by": "coordinator",
            "input_evidence_refs": [],
        }
        append_event(
            board,
            EventType.TASK_ASSIGNED,
            Ledger.TASK,
            plan,
            "assignment:task-1:1:1",
        )
        clue = append_event(
            board,
            EventType.CLUE,
            Ledger.EVIDENCE,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "clue_id": "clue-1",
            },
            "clue:task-1:clue-1",
        )
        client = BlackboardClient(board)
        task_view = client.query_view(
            "task",
            {
                "view_type": "task_coordination",
                "task_id": "task-1",
                "task_revision": 1,
            },
            board.high_watermark(),
            0,
        )
        self.assertTrue(task_view["replan_pending"])
        self.assertEqual([clue.id], task_view["pending_replan_cause_ids"])
        evidence = client.query_view(
            "evidence",
            {"view_type": "evidence", "task_id": "task-1"},
            board.high_watermark(),
            0,
        )
        self.assertEqual(clue.id, evidence["clues_by_key"]["clue-1"]["event_id"])

        append_event(
            board,
            EventType.TASK_REPLAN,
            Ledger.TASK,
            {
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "cause_event_id": clue.id,
                "from_epoch": 1,
                "to_epoch": 2,
                "evidence_refs": [clue.id],
            },
            "replan:task-1:1:clue-1",
        )
        # Completion from the old plan must not leak into epoch 2.
        stale_completion = BlackboardEvent(
            id="assignment-completed:old",
            type=EventType.ASSIGNMENT_COMPLETED,
            ledger=Ledger.TASK,
            content={
                "schema_version": 2,
                "task_id": "task-1",
                "task_revision": 1,
                "coordination_epoch": 1,
                "plan_id": "plan:task-1:1:1",
                "assignment_id": "task-1:search",
                "assignment_epoch": 1,
                "device_id": "dog-a",
                "final_progress": 1.0,
                "receipt_id": "receipt:old",
                "completion_evidence": ["receipt-event:old"],
                "completed_at": 100.0,
            },
            source="coordinator",
            idempotency_key="assignment-completed:old",
        )
        watermark_before_stale = board.high_watermark()
        stale_result = board.append(stale_completion)
        self.assertEqual(AppendStatus.APPENDED, stale_result.status)
        self.assertEqual(watermark_before_stale + 1, board.high_watermark())
        replanned = client.query_view(
            "task",
            {
                "view_type": "task_coordination",
                "task_id": "task-1",
                "task_revision": 1,
            },
            board.high_watermark(),
            0,
        )
        self.assertEqual(2, replanned["coordination_epoch"])
        self.assertFalse(replanned["replan_pending"])
        self.assertIsNone(replanned["current_plan"])
        self.assertEqual([], replanned["completed_assignment_ids"])

    def test_registry_projects_agent_public_without_telemetry_events(self) -> None:
        clock = ManualClock()
        registry = Registry()
        card = AgentCard(
            identity=DeviceRef("dog-a", DeviceType.DOG),
            state=DeviceState(
                battery=0.9,
                endurance_s=600,
                online=True,
                healthy=True,
                extra={"state_updated_at": 100.0},
            ),
            capability=CapabilitySlot(
                action_verbs=[ActionVerb.MOVE_TO],
                profile=CapabilityProfile(
                    capabilities=["search"],
                    constraints=["indoor_only"],
                    width_cm=40,
                ),
            ),
        )
        registry.register(card)
        board = Blackboard(clock=clock, agent_snapshot_provider=registry)
        append_event(
            board,
            EventType.TASK_POSTED,
            Ledger.TASK,
            task_payload(),
            "task-posted:task-1:1",
        )
        client = BlackboardClient(board)
        single = client.query_view(
            None,
            {"view_type": "agent_public", "device_id": "dog-a"},
            1,
            0,
        )
        self.assertEqual("dog-a", single["device_id"])
        self.assertEqual(["search"], single["capabilities"])
        self.assertEqual(["move_to"], single["action_verbs"])
        self.assertEqual(100.0, single["state_updated_at"])
        self.assertEqual(1, single["view_version"])

        multiple = client.query_view(
            None,
            {
                "view_type": "agent_public",
                "device_ids": ["dog-a", "missing"],
            },
            1,
            0,
        )
        self.assertEqual(1, multiple["view_version"])
        self.assertIn("dog-a", multiple)
        self.assertNotIn("missing", multiple)


class RealCoordinationBridgeTest(unittest.TestCase):
    def test_structurally_safe_invalid_task_converges_to_explicit_failure(self) -> None:
        clock = ManualClock()
        board = Blackboard(clock=clock)
        client = BlackboardClient(board)
        posted = make_blackboard_event(
            CoordinationEventType.TASK_POSTED,
            Ledger.TASK,
            {
                "schema_version": 2,
                "task_id": "task-without-roles",
                "task_revision": 1,
                # Coordinator owns the business decision that no role slots is
                # a terminal task error; Blackboard only enforces parse safety.
            },
            "ingress",
            "task-posted:task-without-roles:1",
        )
        self.assertEqual("appended", client.append(posted).status.value)
        with tempfile.TemporaryDirectory() as root:
            coordinator = Coordinator(
                blackboard=client,
                store=FileCoordinatorStore(root),
                bid_window_s=10,
            )
            self.assertEqual(1, coordinator.poll_once(timeout_s=0))

        terminal = client.query_view(
            Ledger.TASK.value,
            {
                "view_type": "terminal",
                "task_id": "task-without-roles",
                "task_revision": 1,
            },
            board.high_watermark(),
            0,
        )
        self.assertEqual("failed", terminal["status"])
        self.assertEqual(
            "ROLE_SLOTS_REQUIRED",
            terminal["terminal_event"]["payload"]["reason_code"],
        )

    def test_real_board_runs_the_coordination_closed_loop_once(self) -> None:
        clock = ManualClock()
        board = Blackboard(clock=clock)
        board.upsert_agent_snapshot(public_snapshot())
        client = BlackboardClient(board)
        task = task_payload()
        posted = make_blackboard_event(
            CoordinationEventType.TASK_POSTED,
            Ledger.TASK,
            task,
            "ingress",
            "task-posted:task-1:1",
        )
        self.assertEqual("appended", client.append(posted).status.value)

        gateway = MockPhysicalActionGateway()
        with tempfile.TemporaryDirectory() as root:
            coordinator = Coordinator(
                blackboard=client,
                store=FileCoordinatorStore(root),
                bid_window_s=10,
            )
            host = AgentProcessHost(
                spec=AgentProcessSpec("dog-a", "memory", root),
                blackboard=client,
                loop=PureAgentLoop("dog-a"),
                session_store=FileTaskSessionStore(root, "dog-a"),
                action_executor=ActionExecutor(
                    safety=StaticSafetyPort(), gateway=gateway
                ),
                action_gateway=gateway,
                skill_provider=NullSkillReferenceProvider(),
            )

            self.assertEqual(1, coordinator.poll_once(timeout_s=0))
            self.assertEqual(1, host.poll_once(timeout_s=0))
            self.assertEqual(2, coordinator.poll_once(timeout_s=0))

            clock.value = 111.0
            assigned_outputs = coordinator.tick()
            self.assertEqual(
                ["task_assigned"],
                [event_type_value(event) for event in assigned_outputs],
            )
            assignment_event = copy.deepcopy(assigned_outputs[0])

            self.assertEqual(1, host.poll_once(timeout_s=0))
            self.assertEqual(1, len(gateway.dispatched_intent_ids))
            self.assertEqual(2, coordinator.poll_once(timeout_s=0))
            host.poll_once(timeout_s=0)

            final = client.query_view(
                "task",
                {
                    "view_type": "task_coordination",
                    "task_id": "task-1",
                    "task_revision": 1,
                },
                board.high_watermark(),
                0,
            )
            self.assertEqual("done", final["status"])
            terminal = client.query_view(
                "task",
                {
                    "view_type": "terminal",
                    "task_id": "task-1",
                    "task_revision": 1,
                },
                board.high_watermark(),
                0,
            )
            self.assertEqual("task_done", terminal["terminal_event_type"])

            # Assignment replay is an append duplicate, so the Host sees no new input.
            self.assertEqual("duplicate_same", client.append(assignment_event).status.value)
            self.assertEqual(0, host.poll_once(timeout_s=0))
            self.assertEqual(1, len(gateway.dispatched_intent_ids))


if __name__ == "__main__":
    unittest.main()
