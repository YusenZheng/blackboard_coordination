# STATUS: STAGED(A类)—— 竞标、组队、正式提交、完成/失败和简单重规划
"""控制进程中的 Coordinator。

Coordinator 只消费 Blackboard 事实并写回协同决策。Group Policy 只能给候选计划，
正式 TASK_ASSIGNED 必须经过确定性校验；模型失败时使用最大匹配兜底。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..contracts.blackboard_event import BlackboardEvent, Ledger
from .file_session_store import FileCoordinatorStore
from .models import (
    AgentPublicSnapshot,
    AppendResult,
    AppendStatus,
    AssignmentPlanSnapshot,
    CoordinationEventType,
    EventEnvelope,
    GroupPlanningInput,
    InvalidPlan,
    RoleSlot,
    ValidPlan,
    event_content,
    event_type_value,
    fingerprint,
    make_blackboard_event,
    to_json_value,
)
from .policy import (
    DeterministicGroupPlanningPolicy,
    MinimalPlanValidator,
    deterministic_maximum_matching,
    parse_bid_record,
)
from .ports import BlackboardClientPort, GroupPlanningPolicyPort
from .publisher import DurableEventPublisher


COORDINATOR_EVENT_TYPES = {
    CoordinationEventType.TASK_POSTED.value,
    CoordinationEventType.BID_ROUND_OPENED.value,
    CoordinationEventType.BID.value,
    CoordinationEventType.TASK_ASSIGNED.value,
    CoordinationEventType.RECEIPT.value,
    CoordinationEventType.SAFETY_INTERCEPT.value,
    CoordinationEventType.ASSIGNMENT_COMPLETED.value,
    CoordinationEventType.CLUE.value,
    CoordinationEventType.TASK_REPLAN.value,
    CoordinationEventType.ESTOP.value,
    CoordinationEventType.TASK_DONE.value,
    CoordinationEventType.TASK_FAILED.value,
}


@dataclass
class ActiveBidRound:
    task_id: str
    task_revision: int
    coordination_epoch: int
    bid_round: int
    deadline: float


class Coordinator:
    def __init__(
        self,
        *,
        blackboard: BlackboardClientPort,
        store: FileCoordinatorStore,
        group_policy: Optional[GroupPlanningPolicyPort] = None,
        validator: Optional[MinimalPlanValidator] = None,
        bid_window_s: float = 10.0,
        group_policy_timeout_s: float = 5.0,
        view_timeout_s: float = 5.0,
    ) -> None:
        self.blackboard = blackboard
        self.store = store
        self.group_policy = group_policy or DeterministicGroupPlanningPolicy()
        self.validator = validator or MinimalPlanValidator()
        self.bid_window_s = bid_window_s
        self.group_policy_timeout_s = group_policy_timeout_s
        self.view_timeout_s = view_timeout_s
        self._active_rounds: dict[str, ActiveBidRound] = {}
        self._known_tasks: set[tuple[str, int]] = set()
        self._published_during_handle: set[str] = set()
        self._current_input_offset: Optional[int] = None
        self._pending_commit_keys: list[str] = []
        self._last_handle_commit_keys: list[str] = []
        self._control_state_recovered = False
        self.publisher = DurableEventPublisher(
            blackboard,
            lambda event: store.stage_event(
                event, source_offset=self._current_input_offset
            ),
            store.resolve_event,
            store.reject_event,
            auto_resolve_success=False,
        )

    def replay_outbox(self) -> int:
        count = 0
        committed_offset = self.store.load_consumer_offset()
        for record in self.store.list_event_records():
            if record.source_offset is not None and record.source_offset > committed_offset:
                continue
            event = record.event
            result = self.publisher.publish(event)
            if result.status == AppendStatus.TERMINAL_CONFLICT:
                self._confirm_terminal_conflict(event, result)
            if event.idempotency_key and result.status in (
                AppendStatus.APPENDED,
                AppendStatus.DUPLICATE_SAME,
                AppendStatus.TERMINAL_CONFLICT,
            ):
                self.store.resolve_event(event.idempotency_key)
            count += 1
        return count

    def recover_control_state(self, *, limit: int = 100) -> int:
        """Rebuild timers and task catalogue from the authoritative event history."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        self._active_rounds.clear()
        self._known_tasks.clear()
        cursor = -1
        recovered = 0
        recovery_types = {
            CoordinationEventType.TASK_POSTED.value,
            CoordinationEventType.BID_ROUND_OPENED.value,
            CoordinationEventType.TASK_ASSIGNED.value,
            CoordinationEventType.TASK_REPLAN.value,
            CoordinationEventType.TASK_DONE.value,
            CoordinationEventType.TASK_FAILED.value,
        }
        while True:
            values = self.blackboard.read_since(
                cursor, recovery_types, limit, 0.0
            )
            if not values:
                break
            for value in values:
                envelope = _normalize_envelope(value)
                content = event_content(envelope.event)
                event_type = event_type_value(envelope.event)
                task_id = str(content.get("task_id", ""))
                revision = int(content.get("task_revision", 1))
                if task_id:
                    self._known_tasks.add((task_id, revision))
                if event_type == CoordinationEventType.BID_ROUND_OPENED.value:
                    self._remember_bid_round(content)
                elif event_type in (
                    CoordinationEventType.TASK_ASSIGNED.value,
                    CoordinationEventType.TASK_REPLAN.value,
                    CoordinationEventType.TASK_DONE.value,
                    CoordinationEventType.TASK_FAILED.value,
                ):
                    self._active_rounds.pop(task_id, None)
                if event_type in (
                    CoordinationEventType.TASK_DONE.value,
                    CoordinationEventType.TASK_FAILED.value,
                ):
                    self._known_tasks.discard((task_id, revision))
                cursor = envelope.offset
                recovered += 1
            if len(values) < limit:
                break
        self._control_state_recovered = True
        return recovered

    def poll_once(self, *, limit: int = 100, timeout_s: float = 0.1) -> int:
        if not self._control_state_recovered:
            self.recover_control_state(limit=limit)
            self.replay_outbox()
        last_offset = self.store.load_consumer_offset()
        values = self.blackboard.read_since(
            last_offset, COORDINATOR_EVENT_TYPES, limit, timeout_s
        )
        processed = 0
        for value in values:
            envelope = _normalize_envelope(value)
            self.handle(envelope)
            self.store.save_consumer_offset(envelope.offset)
            for key in self._last_handle_commit_keys:
                self.store.resolve_event(key)
            self._last_handle_commit_keys = []
            processed += 1
        self.tick()
        return processed

    def handle(self, envelope: EventEnvelope) -> list[BlackboardEvent]:
        self._published_during_handle = set()
        self._pending_commit_keys = []
        self._current_input_offset = envelope.offset
        try:
            event = envelope.event
            event_type = event_type_value(event)
            content = event_content(event)
            task_id = str(content.get("task_id", ""))
            revision = int(content.get("task_revision", 1))
            if task_id:
                self._known_tasks.add((task_id, revision))

            outputs: list[BlackboardEvent] = []
            if event_type == CoordinationEventType.TASK_POSTED.value:
                outputs.extend(self._on_task_posted(event))
            elif event_type == CoordinationEventType.BID_ROUND_OPENED.value:
                self._remember_bid_round(content)
            elif event_type == CoordinationEventType.TASK_ASSIGNED.value:
                self._active_rounds.pop(task_id, None)
            elif event_type == CoordinationEventType.RECEIPT.value:
                outputs.extend(self._on_receipt(event))
            elif event_type == CoordinationEventType.SAFETY_INTERCEPT.value:
                outputs.append(
                    self._task_failed_event(
                        task_id=task_id,
                        task_revision=revision,
                        coordination_epoch=int(content.get("coordination_epoch", 0)),
                        reason_code="SAFETY_BLOCKED",
                        failed_component="safety",
                        related_event_ids=[event.id],
                    )
                )
            elif event_type == CoordinationEventType.CLUE.value:
                outputs.extend(self._on_clue(event))
            elif event_type == CoordinationEventType.ESTOP.value:
                outputs.extend(self._on_estop(event))
            elif event_type in (
                CoordinationEventType.TASK_DONE.value,
                CoordinationEventType.TASK_FAILED.value,
            ):
                self._active_rounds.pop(task_id, None)
                self._known_tasks.discard((task_id, revision))

            for output in outputs:
                if output.id in self._published_during_handle:
                    continue
                self._publish(output)
            self._last_handle_commit_keys = list(self._pending_commit_keys)
            return outputs
        except Exception:
            self._last_handle_commit_keys = []
            raise
        finally:
            self._pending_commit_keys = []
            self._current_input_offset = None

    def tick(self) -> list[BlackboardEvent]:
        outputs: list[BlackboardEvent] = []
        now = self.blackboard.now()
        due = sorted(
            (
                round_state
                for round_state in self._active_rounds.values()
                if now > round_state.deadline
            ),
            key=lambda item: (item.deadline, item.task_id),
        )
        for round_state in due:
            output = self.close_bid_round(round_state)
            if output is not None:
                self._publish(output)
                outputs.append(output)
            self._active_rounds.pop(round_state.task_id, None)
        return outputs

    def close_bid_round(self, round_state: ActiveBidRound) -> Optional[BlackboardEvent]:
        barrier = self.blackboard.high_watermark()
        task_view, planning_input = self._planning_input(round_state, barrier)
        if planning_input is None:
            return None
        if not planning_input.role_slots:
            return self._task_failed_event(
                task_id=round_state.task_id,
                task_revision=round_state.task_revision,
                coordination_epoch=round_state.coordination_epoch,
                reason_code="ROLE_SLOTS_REQUIRED",
                failed_component="coordinator",
                related_event_ids=[],
            )

        candidate = None
        try:
            candidate = self.group_policy.plan(
                planning_input, self.group_policy_timeout_s
            )
        except Exception:
            # Group Policy/LLM 是可降级组件；Validator 的实现错误不能在这里吞掉。
            candidate = None

        # 模型调用期间 Blackboard 可能推进。正式提交前重新读取所有权威输入；
        # 输入变化时放弃模型候选，直接对新快照执行确定性 fallback。
        fresh_barrier = self.blackboard.high_watermark()
        _, fresh_input = self._planning_input(round_state, fresh_barrier)
        if fresh_input is None:
            return None
        if not fresh_input.role_slots:
            return self._task_failed_event(
                task_id=round_state.task_id,
                task_revision=round_state.task_revision,
                coordination_epoch=round_state.coordination_epoch,
                reason_code="ROLE_SLOTS_REQUIRED",
                failed_component="coordinator",
                related_event_ids=[],
            )
        if fresh_input.input_fingerprint != planning_input.input_fingerprint:
            candidate = None
        planning_input = fresh_input

        validation = (
            self.validator.validate(candidate, planning_input)
            if candidate is not None
            else InvalidPlan([])
        )
        if not isinstance(validation, ValidPlan):
            candidate = deterministic_maximum_matching(planning_input)
            validation = (
                self.validator.validate(candidate, planning_input)
                if candidate is not None
                else InvalidPlan([])
            )
        if not isinstance(validation, ValidPlan):
            only_channel_failures = bool(planning_input.bids) and all(
                all(
                    offer.decision == "reject"
                    and offer.reason_codes == ["EXECUTION_CHANNEL_UNAVAILABLE"]
                    for offer in record.payload.offers
                )
                for record in planning_input.bids
            )
            return self._task_failed_event(
                task_id=round_state.task_id,
                task_revision=round_state.task_revision,
                coordination_epoch=round_state.coordination_epoch,
                reason_code=(
                    "EXECUTION_CHANNEL_UNAVAILABLE"
                    if only_channel_failures
                    else "NO_FEASIBLE_TEAM"
                ),
                failed_component="coordinator",
                related_event_ids=[record.event_id for record in planning_input.bids],
            )
        return self._task_assigned_event(validation.assignment_plan)

    def _on_task_posted(self, event: BlackboardEvent) -> list[BlackboardEvent]:
        content = event_content(event)
        task_id = str(content.get("task_id", ""))
        revision = int(content.get("task_revision", 0))
        role_slots = content.get("role_slots", [])
        if not task_id or revision < 1:
            return [
                self._task_failed_event(
                    task_id=task_id or "invalid-task",
                    task_revision=max(1, revision),
                    coordination_epoch=0,
                    reason_code="TASK_INPUT_INVALID",
                    failed_component="ingress/coordinator",
                    related_event_ids=[event.id],
                )
            ]
        if not role_slots:
            return [
                self._task_failed_event(
                    task_id=task_id,
                    task_revision=revision,
                    coordination_epoch=0,
                    reason_code="ROLE_SLOTS_REQUIRED",
                    failed_component="coordinator",
                    related_event_ids=[event.id],
                )
            ]
        task_view = self._task_view(task_id, revision, event.version)
        if task_view.get("terminal_event_id") is not None:
            return []
        epoch = max(1, int(task_view.get("coordination_epoch", 0)) + 1)
        return [self._bid_round_event(task_id, revision, epoch, role_slots)]

    def _on_receipt(self, event: BlackboardEvent) -> list[BlackboardEvent]:
        content = event_content(event)
        task_id = str(content.get("task_id", ""))
        revision = int(content.get("task_revision", 0))
        epoch = int(content.get("coordination_epoch", 0))
        task_view = self._task_view(task_id, revision, event.version)
        plan = task_view.get("current_plan")
        if not isinstance(plan, dict) or int(plan.get("coordination_epoch", 0)) != epoch:
            return []
        if (
            content.get("success") is not True
            or content.get("outcome_certainty") != "confirmed"
            or content.get("post_check_allowed") is not True
        ):
            reason_code = (
                "DISPATCH_UNKNOWN"
                if content.get("outcome_certainty") == "unknown"
                else "ACTION_FAILED"
            )
            return [
                self._task_failed_event(
                    task_id=task_id,
                    task_revision=revision,
                    coordination_epoch=epoch,
                    reason_code=reason_code,
                    failed_component="action_executor",
                    related_event_ids=[event.id],
                )
            ]
        assignment = next(
            (
                value
                for value in plan.get("assignments", [])
                if value.get("assignment_id") == content.get("assignment_id")
                and int(value.get("assignment_epoch", 0))
                == int(content.get("assignment_epoch", -1))
            ),
            None,
        )
        if assignment is None:
            return []
        required_fields = assignment.get("completion_rule", {}).get(
            "required_result_fields", []
        )
        if not all(_has_path(content.get("result", {}), path) for path in required_fields):
            return [
                self._task_failed_event(
                    task_id=task_id,
                    task_revision=revision,
                    coordination_epoch=epoch,
                    reason_code="COMPLETION_RULE_FAILED",
                    failed_component="coordinator",
                    related_event_ids=[event.id],
                )
            ]
        completion = self._assignment_completed_event(content, event.id)
        result = self._publish(completion)
        self._published_during_handle.add(completion.id)
        outputs = [completion]
        if result.status == AppendStatus.TERMINAL_CONFLICT:
            return outputs
        min_version = result.version or event.version
        refreshed = self._task_view(task_id, revision, min_version)
        if refreshed.get("replan_pending"):
            replan = self._make_replan_event(refreshed)
            if replan is not None:
                outputs.extend([replan, self._round_after_replan(replan, refreshed)])
            return outputs
        current_plan = refreshed.get("current_plan", plan)
        expected_ids = {
            value.get("assignment_id") for value in current_plan.get("assignments", [])
        }
        completed_ids = set(refreshed.get("completed_assignment_ids", []))
        if expected_ids and expected_ids.issubset(completed_ids):
            outputs.append(self._task_done_event(refreshed))
        return outputs

    def _on_clue(self, event: BlackboardEvent) -> list[BlackboardEvent]:
        content = event_content(event)
        task_id = str(content.get("task_id", ""))
        if not task_id:
            return []
        revision = int(
            content.get("task_revision") or self._latest_known_revision(task_id) or 1
        )
        task_view = self._task_view(task_id, revision, event.version)
        if not task_view.get("replan_pending"):
            return []
        replan = self._make_replan_event(task_view)
        if replan is None:
            return []
        return [replan, self._round_after_replan(replan, task_view)]

    def _on_estop(self, event: BlackboardEvent) -> list[BlackboardEvent]:
        content = event_content(event)
        if content.get("operation") != "stop":
            return []
        task_id = content.get("task_id")
        if task_id:
            revision = int(content.get("task_revision", 1))
            view = self._task_view(str(task_id), revision, event.version)
            if view.get("terminal_event_id") is None:
                return [
                    self._task_failed_event(
                        task_id=str(task_id),
                        task_revision=revision,
                        coordination_epoch=int(view.get("coordination_epoch", 0)),
                        reason_code="ESTOP_TRIGGERED",
                        failed_component="safety",
                        related_event_ids=[event.id],
                    )
                ]
            return []
        outputs = []
        for known_task_id, revision in sorted(self._known_tasks):
            view = self._task_view(known_task_id, revision, event.version)
            if view.get("terminal_event_id") is None and _estop_affects_plan(content, view):
                outputs.append(
                    self._task_failed_event(
                        task_id=known_task_id,
                        task_revision=revision,
                        coordination_epoch=int(view.get("coordination_epoch", 0)),
                        reason_code="ESTOP_TRIGGERED",
                        failed_component="safety",
                        related_event_ids=[event.id],
                    )
                )
        return outputs

    def fail_for_agent_exit(self, device_id: str, related_event_ids: Optional[list[str]] = None) -> list[BlackboardEvent]:
        outputs = []
        for task_id, revision in sorted(self._known_tasks):
            view = self._task_view(task_id, revision, self.blackboard.high_watermark())
            plan = view.get("current_plan") or {}
            if view.get("terminal_event_id") is None and any(
                item.get("device_id") == device_id for item in plan.get("assignments", [])
            ):
                event = self._task_failed_event(
                    task_id=task_id,
                    task_revision=revision,
                    coordination_epoch=int(view.get("coordination_epoch", 0)),
                    reason_code="AGENT_PROCESS_EXITED",
                    failed_component="supervisor",
                    related_event_ids=related_event_ids or [],
                )
                self._publish(event)
                outputs.append(event)
        return outputs

    def _bid_round_event(
        self,
        task_id: str,
        revision: int,
        epoch: int,
        role_slots: list[dict[str, Any]],
    ) -> BlackboardEvent:
        content = {
            "schema_version": 2,
            "task_id": task_id,
            "task_revision": revision,
            "coordination_epoch": epoch,
            "bid_round": 1,
            "slots": [str(slot["slot_id"]) for slot in role_slots],
            "deadline": self.blackboard.now() + self.bid_window_s,
        }
        return make_blackboard_event(
            CoordinationEventType.BID_ROUND_OPENED,
            Ledger.TASK,
            content,
            "coordinator",
            f"bid-round:{task_id}:{revision}:{epoch}:1",
        )

    def _remember_bid_round(self, content: dict[str, Any]) -> None:
        round_state = ActiveBidRound(
            task_id=str(content["task_id"]),
            task_revision=int(content["task_revision"]),
            coordination_epoch=int(content["coordination_epoch"]),
            bid_round=int(content.get("bid_round", 1)),
            deadline=float(content["deadline"]),
        )
        self._active_rounds[round_state.task_id] = round_state

    @staticmethod
    def _task_assigned_event(plan: AssignmentPlanSnapshot) -> BlackboardEvent:
        return make_blackboard_event(
            CoordinationEventType.TASK_ASSIGNED,
            Ledger.TASK,
            plan,
            "coordinator",
            f"assignment:{plan.task_id}:{plan.task_revision}:{plan.coordination_epoch}",
        )

    def _assignment_completed_event(
        self, receipt: dict[str, Any], receipt_event_id: str
    ) -> BlackboardEvent:
        now = self.blackboard.now()
        content = {
            "schema_version": 2,
            "task_id": receipt["task_id"],
            "task_revision": receipt["task_revision"],
            "coordination_epoch": receipt["coordination_epoch"],
            "plan_id": receipt["plan_id"],
            "assignment_id": receipt["assignment_id"],
            "assignment_epoch": receipt["assignment_epoch"],
            "device_id": receipt["device_id"],
            "final_progress": 1.0,
            "receipt_id": receipt["receipt_id"],
            "completion_evidence": [receipt_event_id],
            "completed_at": now,
        }
        return make_blackboard_event(
            CoordinationEventType.ASSIGNMENT_COMPLETED,
            Ledger.TASK,
            content,
            "coordinator",
            (
                f"assignment-completed:{receipt['task_id']}:{receipt['task_revision']}:"
                f"{receipt['coordination_epoch']}:{receipt['assignment_id']}:"
                f"{receipt['assignment_epoch']}"
            ),
        )

    def _task_done_event(self, task_view: dict[str, Any]) -> BlackboardEvent:
        plan = task_view["current_plan"]
        task_id = str(task_view["task_id"])
        revision = int(task_view["task_revision"])
        content = {
            "schema_version": 2,
            "task_id": task_id,
            "task_revision": revision,
            "coordination_epoch": int(task_view["coordination_epoch"]),
            "plan_id": plan["plan_id"],
            "completed_assignment_ids": sorted(task_view["completed_assignment_ids"]),
            "completion_evidence": [],
            "completed_at": self.blackboard.now(),
        }
        return make_blackboard_event(
            CoordinationEventType.TASK_DONE,
            Ledger.TASK,
            content,
            "coordinator",
            f"task-terminal:{task_id}:{revision}",
        )

    def _task_failed_event(
        self,
        *,
        task_id: str,
        task_revision: int,
        coordination_epoch: int,
        reason_code: str,
        failed_component: str,
        related_event_ids: list[str],
    ) -> BlackboardEvent:
        content = {
            "schema_version": 2,
            "task_id": task_id,
            "task_revision": task_revision,
            "coordination_epoch": coordination_epoch,
            "reason_code": reason_code,
            "failed_component": failed_component,
            "related_event_ids": list(related_event_ids),
            "retryable": False,
            "failed_at": self.blackboard.now(),
        }
        return make_blackboard_event(
            CoordinationEventType.TASK_FAILED,
            Ledger.TASK,
            content,
            "coordinator",
            f"task-terminal:{task_id}:{task_revision}",
        )

    def _make_replan_event(self, task_view: dict[str, Any]) -> Optional[BlackboardEvent]:
        pending = list(task_view.get("pending_replan_cause_ids", []))
        if not pending or task_view.get("terminal_event_id") is not None:
            return None
        action_view = self.blackboard.query_view(
            Ledger.RECEIPT.value,
            {
                "view_type": "action",
                "task_id": task_view["task_id"],
                "task_revision": task_view["task_revision"],
            },
            task_view.get("view_version"),
            self.view_timeout_s,
        )
        if _has_inflight_intent(task_view, dict(action_view or {})):
            return None
        evidence_view = self._evidence_view(
            str(task_view["task_id"]), int(task_view.get("view_version", 0))
        ) or {}
        clues = evidence_view.get("clues_by_key", {})
        event_order = {
            item.get("event_id"): (int(item.get("event_version", 0)), str(item.get("event_id", "")))
            for item in clues.values()
            if isinstance(item, dict) and item.get("event_id")
        }
        cause_event_id = min(
            pending, key=lambda event_id: event_order.get(event_id, (2**63 - 1, event_id))
        )
        from_epoch = int(task_view["coordination_epoch"])
        content = {
            "schema_version": 2,
            "task_id": task_view["task_id"],
            "task_revision": task_view["task_revision"],
            "cause_event_id": cause_event_id,
            "cause_type": "clue",
            "from_epoch": from_epoch,
            "to_epoch": from_epoch + 1,
            "reason_code": "NEW_CLUE",
            "evidence_refs": pending,
        }
        return make_blackboard_event(
            CoordinationEventType.TASK_REPLAN,
            Ledger.TASK,
            content,
            "coordinator",
            f"replan:{task_view['task_id']}:{task_view['task_revision']}:{cause_event_id}",
        )

    def _round_after_replan(
        self, replan_event: BlackboardEvent, task_view: dict[str, Any]
    ) -> BlackboardEvent:
        content = event_content(replan_event)
        task = dict(task_view["task"])
        return self._bid_round_event(
            str(content["task_id"]),
            int(content["task_revision"]),
            int(content["to_epoch"]),
            list(task["role_slots"]),
        )

    def _planning_input(
        self, round_state: ActiveBidRound, barrier: int
    ) -> tuple[dict[str, Any], Optional[GroupPlanningInput]]:
        task_view = self._task_view(
            round_state.task_id, round_state.task_revision, barrier
        )
        if task_view.get("terminal_event_id") is not None:
            return task_view, None
        if int(task_view.get("task_revision", 0)) != round_state.task_revision:
            return task_view, None
        if int(task_view.get("coordination_epoch", 0)) != round_state.coordination_epoch:
            return task_view, None
        # 当前轮已经被重规划请求取代时，不得提交旧候选计划。
        if task_view.get("replan_pending"):
            return task_view, None
        bid_view = self.blackboard.query_view(
            Ledger.TASK.value,
            {
                "view_type": "bid_round",
                "task_id": round_state.task_id,
                "task_revision": round_state.task_revision,
                "coordination_epoch": round_state.coordination_epoch,
                "bid_round": round_state.bid_round,
            },
            barrier,
            self.view_timeout_s,
        )
        bids = [
            parse_bid_record(value)
            for _, value in sorted(dict(bid_view.get("bids_by_device", {})).items())
        ]
        task = dict(task_view.get("task", {}))
        role_slots = [RoleSlot.from_dict(value) for value in task.get("role_slots", [])]
        device_ids = sorted({record.payload.device_id for record in bids})
        snapshots = self._agent_snapshots(device_ids, barrier)
        evidence_view = self._evidence_view(round_state.task_id, barrier)
        proposals = [record.payload.proposal for record in bids if record.payload.proposal]
        input_base = {
            "task": task,
            "role_slots": role_slots,
            "bids": bids,
            "proposals": proposals,
            "agent_snapshots": snapshots,
            "evidence_view": evidence_view,
            "task_revision": round_state.task_revision,
            "coordination_epoch": round_state.coordination_epoch,
        }
        planning_input = GroupPlanningInput(
            task=task,
            role_slots=role_slots,
            bids=bids,
            proposals=proposals,
            agent_snapshots=snapshots,
            evidence_view=evidence_view,
            task_revision=round_state.task_revision,
            coordination_epoch=round_state.coordination_epoch,
            input_fingerprint=fingerprint(input_base),
        )
        return task_view, planning_input

    def _task_view(self, task_id: str, revision: int, min_version: int) -> dict:
        return dict(
            self.blackboard.query_view(
                Ledger.TASK.value,
                {
                    "view_type": "task_coordination",
                    "task_id": task_id,
                    "task_revision": revision,
                },
                min_version,
                self.view_timeout_s,
            )
            or {}
        )

    def _agent_snapshots(
        self, device_ids: list[str], min_version: int
    ) -> list[AgentPublicSnapshot]:
        value = self.blackboard.query_view(
            None,
            {"view_type": "agent_public", "device_ids": device_ids},
            min_version,
            self.view_timeout_s,
        )
        if isinstance(value, dict) and "device_id" not in value:
            values = [value[key] for key in device_ids if key in value]
        elif isinstance(value, list):
            values = value
        else:
            values = [value] if value else []
        return [AgentPublicSnapshot.from_dict(dict(item)) for item in values]

    def _evidence_view(self, task_id: str, min_version: int) -> Optional[dict]:
        try:
            return dict(
                self.blackboard.query_view(
                    Ledger.EVIDENCE.value,
                    {"view_type": "evidence", "task_id": task_id},
                    min_version,
                    self.view_timeout_s,
                )
                or {}
            )
        except (KeyError, NotImplementedError):
            return None

    def _publish(self, event: BlackboardEvent) -> AppendResult:
        result = self.publisher.publish(event)
        if result.status == AppendStatus.TERMINAL_CONFLICT:
            self._confirm_terminal_conflict(event, result)
        if event.idempotency_key and result.status in (
            AppendStatus.APPENDED,
            AppendStatus.DUPLICATE_SAME,
            AppendStatus.TERMINAL_CONFLICT,
        ):
            if self._current_input_offset is None:
                self.store.resolve_event(event.idempotency_key)
            else:
                self._pending_commit_keys.append(event.idempotency_key)
        return result

    def _confirm_terminal_conflict(
        self, event: BlackboardEvent, result: AppendResult
    ) -> None:
        content = event_content(event)
        self.blackboard.query_view(
            Ledger.TASK.value,
            {
                "view_type": "terminal",
                "task_id": content.get("task_id"),
                "task_revision": content.get("task_revision"),
            },
            result.version,
            self.view_timeout_s,
        )

    def _latest_known_revision(self, task_id: str) -> Optional[int]:
        revisions = [revision for known_task_id, revision in self._known_tasks if known_task_id == task_id]
        return max(revisions) if revisions else None


def _normalize_envelope(value: Any) -> EventEnvelope:
    if isinstance(value, EventEnvelope):
        return value
    if isinstance(value, dict):
        return EventEnvelope(offset=int(value["offset"]), event=value["event"])
    return EventEnvelope(offset=int(value.offset), event=value.event)


def _has_path(value: Any, path: str) -> bool:
    current = value
    for part in str(path).split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def _has_inflight_intent(task_view: dict[str, Any], action_view: dict[str, Any]) -> bool:
    plan = task_view.get("current_plan") or {}
    plan_id = plan.get("plan_id")
    receipts = action_view.get("receipts_by_intent", {})
    intercepts = action_view.get("intercepts_by_intent", {})
    for intent_id, intent in action_view.get("intents_by_id", {}).items():
        if intent.get("plan_id") != plan_id:
            continue
        if intent_id in receipts:
            continue
        terminal_intercept = any(
            (
                item.get("check_phase") == "pre"
                and item.get("risk_level") in ("R1", "R2")
            )
            or item.get("check_phase") == "post"
            for item in intercepts.get(intent_id, [])
        )
        if not terminal_intercept:
            return True
    return False


def _estop_affects_plan(content: dict[str, Any], task_view: dict[str, Any]) -> bool:
    scope_type = content.get("scope_type")
    scope_id = content.get("scope_id")
    if scope_type == "all":
        return True
    plan = task_view.get("current_plan") or {}
    if scope_type == "device":
        return any(
            assignment.get("device_id") == scope_id
            for assignment in plan.get("assignments", [])
        )
    return False
