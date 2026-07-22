# STATUS: STAGED(A类)—— 硬资格/BID、Group Policy 接口校验和确定性最大匹配
"""协同层的确定性策略与模型输出护栏。"""
from __future__ import annotations

import math
from typing import Any, Optional

from .models import (
    AgentPublicSnapshot,
    AssignmentPlanSnapshot,
    AssignmentSnapshot,
    BidMetrics,
    BidOffer,
    BidPayload,
    BidRecord,
    CandidateAssignment,
    CandidateAssignmentPlan,
    CollaborationProposal,
    ExecutionAvailability,
    GroupPlanningInput,
    InvalidPlan,
    PlanValidationResult,
    RoleSlot,
    ValidPlan,
    Violation,
    enum_value,
)


class EligibilityBidEngine:
    """Compute hard eligibility. Model output cannot alter this result."""

    def __init__(
        self,
        *,
        agent_state_ttl_s: float = 30.0,
        execution_availability_ttl_s: float = 5.0,
    ) -> None:
        self.agent_state_ttl_s = agent_state_ttl_s
        self.execution_availability_ttl_s = execution_availability_ttl_s

    def make_bid(
        self,
        *,
        task: dict[str, Any],
        role_slots: list[RoleSlot],
        snapshot: AgentPublicSnapshot,
        execution_availability: list[ExecutionAvailability],
        task_revision: int,
        coordination_epoch: int,
        bid_round: int,
        deadline: float,
        now: float,
        proposal: Optional[CollaborationProposal],
    ) -> BidPayload:
        offers = [
            self.evaluate_slot(
                task=task,
                slot=slot,
                snapshot=snapshot,
                execution_availability=execution_availability,
                now=now,
            )
            for slot in role_slots
        ]
        return BidPayload(
            task_id=str(task["task_id"]),
            task_revision=task_revision,
            coordination_epoch=coordination_epoch,
            bid_round=bid_round,
            device_id=snapshot.device_id,
            offers=offers,
            proposal=proposal,
            expires_at=deadline,
        )

    def evaluate_slot(
        self,
        *,
        task: dict[str, Any],
        slot: RoleSlot,
        snapshot: AgentPublicSnapshot,
        execution_availability: list[ExecutionAvailability],
        now: float,
    ) -> BidOffer:
        requirement = dict(task.get("requirement", {}))
        min_battery = float(requirement.get("min_battery", 0.0))
        min_endurance_s = float(requirement.get("min_endurance_s", 0.0))
        capability_match = all(
            capability in snapshot.capabilities for capability in slot.required_capability_ids
        )
        state_fresh = now - snapshot.state_updated_at <= self.agent_state_ttl_s
        online = snapshot.online and state_fresh
        idle = not snapshot.busy or snapshot.busy_task_id == task.get("task_id")
        verb = enum_value(slot.action_template.get("verb", ""))
        availability = next(
            (
                item
                for item in execution_availability
                if item.device_id == snapshot.device_id and enum_value(item.verb) == verb
            ),
            None,
        )
        action_supported = verb in snapshot.action_verbs
        execution_available = bool(
            action_supported
            and
            availability
            and availability.available
            and now - availability.checked_at <= self.execution_availability_ttl_s
        )
        space_ok = _space_constraints_satisfied(
            requirement.get("space_constraints", []), snapshot
        )
        metrics = BidMetrics(
            capability_match=capability_match,
            online=online,
            healthy=snapshot.healthy,
            idle=idle,
            battery_margin=snapshot.battery - min_battery,
            endurance_margin_s=snapshot.endurance_s - min_endurance_s,
            space_constraints_satisfied=space_ok,
            execution_available=execution_available,
            distance_m=_distance_to_task(snapshot.position, task.get("area")),
            reliability=max(0.0, min(1.0, snapshot.success_rate)),
        )
        reasons: list[str] = []
        if not capability_match:
            reasons.append("CAPABILITY_MISMATCH")
        if not state_fresh:
            reasons.append("STALE_AGENT_STATE")
        elif not snapshot.online:
            reasons.append("AGENT_OFFLINE")
        if not snapshot.healthy:
            reasons.append("AGENT_UNHEALTHY")
        if not idle:
            reasons.append("AGENT_BUSY")
        if metrics.battery_margin < 0:
            reasons.append("BATTERY_BELOW_MINIMUM")
        if metrics.endurance_margin_s < 0:
            reasons.append("ENDURANCE_BELOW_MINIMUM")
        if not space_ok:
            reasons.append("SPACE_CONSTRAINT_UNSATISFIED")
        if not execution_available:
            if not action_supported:
                reasons.append("ACTION_VERB_UNSUPPORTED")
            else:
                reasons.append("EXECUTION_CHANNEL_UNAVAILABLE")
        return BidOffer(
            slot_id=slot.slot_id,
            decision="accept" if metrics.eligible else "reject",
            metrics=metrics,
            reason_codes=reasons,
        )


class DeterministicGroupPlanningPolicy:
    """A deterministic policy useful as fallback and as a non-LLM test double."""

    def plan(self, planning_input: GroupPlanningInput, timeout_s: float = 0.0) -> CandidateAssignmentPlan:
        candidate = deterministic_maximum_matching(planning_input)
        if candidate is None:
            return CandidateAssignmentPlan(
                task_id=str(planning_input.task["task_id"]),
                task_revision=planning_input.task_revision,
                coordination_epoch=planning_input.coordination_epoch,
                assignments=[],
                rationale_summary="NO_FEASIBLE_TEAM",
                input_fingerprint=planning_input.input_fingerprint,
            )
        return candidate


class MinimalPlanValidator:
    def validate(
        self,
        candidate: CandidateAssignmentPlan,
        planning_input: GroupPlanningInput,
    ) -> PlanValidationResult:
        violations: list[Violation] = []
        task_id = str(planning_input.task["task_id"])
        if candidate.task_id != task_id:
            violations.append(Violation("TASK_ID_MISMATCH", "candidate task_id is stale"))
        if candidate.task_revision != planning_input.task_revision:
            violations.append(Violation("TASK_REVISION_MISMATCH", "candidate revision is stale"))
        if candidate.coordination_epoch != planning_input.coordination_epoch:
            violations.append(Violation("COORDINATION_EPOCH_MISMATCH", "candidate epoch is stale"))
        if candidate.input_fingerprint != planning_input.input_fingerprint:
            violations.append(Violation("INPUT_FINGERPRINT_MISMATCH", "candidate input is stale"))

        slot_map = {slot.slot_id: slot for slot in planning_input.role_slots}
        candidate_slots = [assignment.slot_id for assignment in candidate.assignments]
        for slot_id in sorted(slot_map):
            count = candidate_slots.count(slot_id)
            if count != 1:
                violations.append(
                    Violation(
                        "SLOT_OWNER_COUNT_INVALID",
                        "every required slot must have exactly one owner",
                        slot_id=slot_id,
                    )
                )
        for unknown_slot in sorted(set(candidate_slots) - set(slot_map)):
            violations.append(
                Violation("UNKNOWN_SLOT", "candidate added a task slot", slot_id=unknown_slot)
            )

        device_ids = [assignment.device_id for assignment in candidate.assignments]
        for device_id in sorted(set(device_ids)):
            if device_ids.count(device_id) > 1:
                violations.append(
                    Violation(
                        "DEVICE_ASSIGNED_MULTIPLE_SLOTS",
                        "one device may own only one slot in the current contract",
                        device_id=device_id,
                    )
                )

        bid_by_event = {record.event_id: record for record in planning_input.bids}
        snapshot_by_device = {
            snapshot.device_id: snapshot for snapshot in planning_input.agent_snapshots
        }
        for assignment in candidate.assignments:
            record = bid_by_event.get(assignment.supporting_bid_event_id)
            if record is None or record.payload.device_id != assignment.device_id:
                violations.append(
                    Violation(
                        "SUPPORTING_BID_INVALID",
                        "assignment must reference the owner's valid BID",
                        assignment.slot_id,
                        assignment.device_id,
                    )
                )
                continue
            offer = _offer_for_slot(record, assignment.slot_id)
            if offer is None or offer.decision != "accept" or not offer.metrics.eligible:
                violations.append(
                    Violation(
                        "OWNER_DID_NOT_ACCEPT_SLOT",
                        "owner has no valid accept offer",
                        assignment.slot_id,
                        assignment.device_id,
                    )
                )
            snapshot = snapshot_by_device.get(assignment.device_id)
            if snapshot is None or not snapshot.online or not snapshot.healthy:
                violations.append(
                    Violation(
                        "OWNER_NO_LONGER_ELIGIBLE",
                        "owner public state is unavailable",
                        assignment.slot_id,
                        assignment.device_id,
                    )
                )
                continue
            slot = slot_map.get(assignment.slot_id)
            requirement = planning_input.task.get("requirement", {})
            if slot is not None and not all(
                capability in snapshot.capabilities
                for capability in slot.required_capability_ids
            ):
                violations.append(
                    Violation(
                        "OWNER_CAPABILITY_STALE",
                        "owner no longer has every required capability",
                        assignment.slot_id,
                        assignment.device_id,
                    )
                )
            if slot is not None and enum_value(
                slot.action_template.get("verb", "")
            ) not in snapshot.action_verbs:
                violations.append(
                    Violation(
                        "OWNER_ACTION_VERB_STALE",
                        "owner no longer exposes the assigned action verb",
                        assignment.slot_id,
                        assignment.device_id,
                    )
                )
            if snapshot.busy and snapshot.busy_task_id != task_id:
                violations.append(
                    Violation(
                        "OWNER_BUSY",
                        "owner is busy on another task",
                        assignment.slot_id,
                        assignment.device_id,
                    )
                )
            if snapshot.battery < float(requirement.get("min_battery", 0.0)):
                violations.append(
                    Violation(
                        "OWNER_BATTERY_STALE",
                        "owner battery is below the task minimum",
                        assignment.slot_id,
                        assignment.device_id,
                    )
                )
            if snapshot.endurance_s < float(requirement.get("min_endurance_s", 0.0)):
                violations.append(
                    Violation(
                        "OWNER_ENDURANCE_STALE",
                        "owner endurance is below the task minimum",
                        assignment.slot_id,
                        assignment.device_id,
                    )
                )
            if not _space_constraints_satisfied(
                requirement.get("space_constraints", []), snapshot
            ):
                violations.append(
                    Violation(
                        "OWNER_SPACE_CONSTRAINT_STALE",
                        "owner no longer satisfies space constraints",
                        assignment.slot_id,
                        assignment.device_id,
                    )
                )

        if violations:
            return InvalidPlan(violations)

        assignment_snapshots: list[AssignmentSnapshot] = []
        for assignment in sorted(candidate.assignments, key=lambda item: item.slot_id):
            slot = slot_map[assignment.slot_id]
            verb = enum_value(slot.action_template.get("verb", ""))
            if verb not in slot.allowed_actions:
                return InvalidPlan(
                    [
                        Violation(
                            "ACTION_NOT_ALLOWED",
                            "action_template verb must be included in allowed_actions",
                            slot.slot_id,
                            assignment.device_id,
                        )
                    ]
                )
            if slot.completion_rule.get("kind") != "receipt_success":
                return InvalidPlan(
                    [
                        Violation(
                            "UNSUPPORTED_COMPLETION_RULE",
                            "only receipt_success is supported",
                            slot.slot_id,
                            assignment.device_id,
                        )
                    ]
                )
            assignment_snapshots.append(
                AssignmentSnapshot(
                    assignment_id=f"{task_id}:{slot.slot_id}",
                    slot_id=slot.slot_id,
                    device_id=assignment.device_id,
                    supporting_bid_event_id=assignment.supporting_bid_event_id,
                    assignment_epoch=planning_input.coordination_epoch,
                    allowed_actions=list(slot.allowed_actions),
                    action_template=dict(slot.action_template),
                    completion_rule=dict(slot.completion_rule),
                )
            )

        evidence_refs = _evidence_refs(planning_input.evidence_view)
        return ValidPlan(
            AssignmentPlanSnapshot(
                task_id=task_id,
                task_revision=planning_input.task_revision,
                coordination_epoch=planning_input.coordination_epoch,
                plan_id=f"plan:{task_id}:{planning_input.task_revision}:{planning_input.coordination_epoch}",
                assignments=assignment_snapshots,
                committed_by="coordinator",
                input_evidence_refs=evidence_refs,
                input_fingerprint=planning_input.input_fingerprint,
            )
        )


def deterministic_maximum_matching(
    planning_input: GroupPlanningInput,
) -> Optional[CandidateAssignmentPlan]:
    """Stable augmenting-path matching; never returns a partial plan."""
    role_slots = sorted(planning_input.role_slots, key=lambda item: item.slot_id)
    edges: dict[str, list[tuple[str, str]]] = {}
    for slot in role_slots:
        values: list[tuple[str, str]] = []
        for record in planning_input.bids:
            offer = _offer_for_slot(record, slot.slot_id)
            if offer is not None and offer.decision == "accept" and offer.metrics.eligible:
                values.append((record.payload.device_id, record.event_id))
        edges[slot.slot_id] = sorted(set(values), key=lambda item: (item[0], item[1]))

    device_to_slot: dict[str, str] = {}
    chosen_bid: dict[tuple[str, str], str] = {}

    def augment(slot_id: str, visited: set[str]) -> bool:
        for device_id, bid_event_id in edges.get(slot_id, []):
            if device_id in visited:
                continue
            visited.add(device_id)
            previous_slot = device_to_slot.get(device_id)
            if previous_slot is None or augment(previous_slot, visited):
                device_to_slot[device_id] = slot_id
                chosen_bid[(slot_id, device_id)] = bid_event_id
                return True
        return False

    for slot in role_slots:
        if not augment(slot.slot_id, set()):
            return None

    slot_to_device = {slot_id: device_id for device_id, slot_id in device_to_slot.items()}
    assignments = [
        CandidateAssignment(
            slot_id=slot.slot_id,
            device_id=slot_to_device[slot.slot_id],
            supporting_bid_event_id=chosen_bid[(slot.slot_id, slot_to_device[slot.slot_id])],
        )
        for slot in role_slots
    ]
    return CandidateAssignmentPlan(
        task_id=str(planning_input.task["task_id"]),
        task_revision=planning_input.task_revision,
        coordination_epoch=planning_input.coordination_epoch,
        assignments=assignments,
        rationale_summary="deterministic maximum matching fallback",
        input_fingerprint=planning_input.input_fingerprint,
    )


def parse_bid_record(value: dict[str, Any]) -> BidRecord:
    payload_value = value.get("payload", value.get("content", value))
    offers = []
    for offer_value in payload_value.get("offers", []):
        metrics_value = offer_value.get("metrics", {})
        metrics = BidMetrics(**metrics_value)
        offers.append(
            BidOffer(
                slot_id=str(offer_value["slot_id"]),
                decision=str(offer_value["decision"]),
                metrics=metrics,
                reason_codes=list(offer_value.get("reason_codes", [])),
            )
        )
    proposal_value = payload_value.get("proposal")
    proposal = CollaborationProposal(**proposal_value) if proposal_value else None
    payload = BidPayload(
        task_id=str(payload_value["task_id"]),
        task_revision=int(payload_value["task_revision"]),
        coordination_epoch=int(payload_value["coordination_epoch"]),
        bid_round=int(payload_value.get("bid_round", 1)),
        device_id=str(payload_value["device_id"]),
        offers=offers,
        proposal=proposal,
        expires_at=float(payload_value["expires_at"]),
        schema_version=int(payload_value.get("schema_version", 2)),
    )
    return BidRecord(
        event_id=str(value.get("event_id", value.get("id", ""))),
        event_version=int(value.get("event_version", value.get("version", 0))),
        event_ts=float(value.get("event_ts", value.get("ts", 0.0))),
        payload=payload,
    )


def _offer_for_slot(record: BidRecord, slot_id: str) -> Optional[BidOffer]:
    return next((offer for offer in record.payload.offers if offer.slot_id == slot_id), None)


def _space_constraints_satisfied(
    constraints: list[dict[str, Any]], snapshot: AgentPublicSnapshot
) -> bool:
    for constraint in constraints:
        min_gap = constraint.get("min_gap_cm")
        if min_gap is not None:
            if snapshot.width_cm is None or snapshot.width_cm > float(min_gap):
                return False
        passable_by = [enum_value(item) for item in constraint.get("passable_by", [])]
        if passable_by and snapshot.device_type not in passable_by:
            return False
    return True


def _distance_to_task(
    position: Optional[dict[str, Any]], area: Optional[dict[str, Any]]
) -> Optional[float]:
    if not position or not area:
        return None
    left_frame = position.get("coordinate_system")
    right_frame = area.get("coordinate_system")
    if left_frame and right_frame and left_frame != right_frame:
        return None
    values = []
    for field_name in ("x_m", "y_m", "z_m"):
        left = position.get(field_name)
        right = area.get(field_name)
        if left is None or right is None:
            if field_name in ("x_m", "y_m"):
                return None
            continue
        values.append((float(left) - float(right)) ** 2)
    return math.sqrt(sum(values)) if values else None


def _evidence_refs(evidence_view: Optional[dict[str, Any]]) -> list[str]:
    if not evidence_view:
        return []
    clues = evidence_view.get("clues_by_key", {})
    keys = evidence_view.get("valid_clue_keys", [])
    return [
        str(clues[key]["event_id"])
        for key in keys
        if key in clues and isinstance(clues[key], dict) and clues[key].get("event_id")
    ]
