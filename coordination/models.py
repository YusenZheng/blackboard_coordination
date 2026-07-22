# STATUS: STAGED(A类)—— 协同层 v2 内部模型；共享事件字段遵循 docs/06 合同
"""协同层内部 DTO、状态和事件构造工具。

本模块不定义 Blackboard、Safety、Access 或 Assets 的实现。现有 contracts 尚未
补齐 v2 事件类型，因此这里保留协调层内部枚举，并在创建 BlackboardEvent 时优先
复用已有 EventType；未进入旧枚举的值按字符串写入，等待 contracts 层统一升级。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, Optional, Union

from ..contracts.blackboard_event import BlackboardEvent, EventType, Ledger
from ..contracts.task import ActionIntent, ActionReceipt
from ..contracts.verbs import ActionVerb


SCHEMA_VERSION = 2


class CoordinationEventType(str, Enum):
    TASK_POSTED = "task_posted"
    BID_ROUND_OPENED = "bid_round_opened"
    BID = "bid"
    TASK_ASSIGNED = "task_assigned"
    ACTION_INTENT = "action_intent"
    SAFETY_INTERCEPT = "safety_intercept"
    RECEIPT = "receipt"
    ASSIGNMENT_COMPLETED = "assignment_completed"
    CLUE = "clue"
    TASK_REPLAN = "task_replan"
    ESTOP = "estop"
    TASK_DONE = "task_done"
    TASK_FAILED = "task_failed"


class AppendStatus(str, Enum):
    APPENDED = "appended"
    DUPLICATE_SAME = "duplicate_same"
    REJECTED = "rejected"
    KEY_CONFLICT = "key_conflict"
    TERMINAL_CONFLICT = "terminal_conflict"


class TaskSessionState(str, Enum):
    OBSERVED = "observed"
    BIDDING = "bidding"
    STANDBY = "standby"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"


class IntentState(str, Enum):
    PROPOSED = "proposed"
    DISPATCHING = "dispatching"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SAFETY_BLOCKED = "safety_blocked"
    DISPATCH_UNKNOWN = "dispatch_unknown"


class ProcessState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPED = "stopped"


class EffectKind(str, Enum):
    APPEND_BLACKBOARD_EVENT = "append_blackboard_event"
    SUBMIT_ACTION_INTENT = "submit_action_intent"
    CLEANUP_TASK_SESSION = "cleanup_task_session"
    NO_EFFECT = "no_effect"


class RiskLevel(str, Enum):
    R0 = "R0"
    R1 = "R1"
    R2 = "R2"


class OutcomeCertainty(str, Enum):
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


@dataclass
class AppendResult:
    status: AppendStatus
    offset: Optional[int]
    version: Optional[int]
    event_id: str
    existing_event_id: Optional[str] = None
    reason_code: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.status in (AppendStatus.APPENDED, AppendStatus.DUPLICATE_SAME)


@dataclass
class EventEnvelope:
    offset: int
    event: BlackboardEvent


@dataclass
class AgentProcessSpec:
    device_id: str
    blackboard_connection_ref: str
    work_root: str
    local_proposal_enabled: bool = False
    local_proposal_timeout_s: float = 3.0


@dataclass
class ProcessStatus:
    device_id: str
    state: ProcessState
    pid: Optional[int]
    last_exit_code: Optional[int]
    reason_code: Optional[str]
    updated_at: float


@dataclass
class RoleSlot:
    slot_id: str
    required_capability_ids: list[str]
    exclusive: bool
    allowed_actions: list[str]
    action_template: dict[str, Any]
    completion_rule: dict[str, Any]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RoleSlot":
        return cls(
            slot_id=str(value.get("slot_id", "")),
            required_capability_ids=list(value.get("required_capability_ids", [])),
            exclusive=bool(value.get("exclusive", True)),
            allowed_actions=[enum_value(item) for item in value.get("allowed_actions", [])],
            action_template=dict(value.get("action_template", {})),
            completion_rule=dict(value.get("completion_rule", {})),
        )


@dataclass
class AgentPublicSnapshot:
    device_id: str
    device_type: str
    online: bool
    healthy: bool
    busy: bool
    busy_task_id: Optional[str]
    battery: float
    endurance_s: float
    position: Optional[dict[str, Any]]
    current_action: str
    capabilities: list[str]
    action_verbs: list[str]
    constraints: list[str]
    width_cm: Optional[float]
    lowest_posture_cm: Optional[float]
    step_limit_cm: Optional[float]
    payload_kg: Optional[float]
    success_rate: float
    card_version: int
    state_updated_at: float
    view_version: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AgentPublicSnapshot":
        return cls(
            device_id=str(value.get("device_id", "")),
            device_type=enum_value(value.get("device_type", "other")),
            online=bool(value.get("online", False)),
            healthy=bool(value.get("healthy", False)),
            busy=bool(value.get("busy", False)),
            busy_task_id=value.get("busy_task_id"),
            battery=float(value.get("battery", 0.0)),
            endurance_s=float(value.get("endurance_s", 0.0)),
            position=value.get("position"),
            current_action=str(value.get("current_action", "")),
            capabilities=list(value.get("capabilities", [])),
            action_verbs=[enum_value(item) for item in value.get("action_verbs", [])],
            constraints=list(value.get("constraints", [])),
            width_cm=_optional_float(value.get("width_cm")),
            lowest_posture_cm=_optional_float(value.get("lowest_posture_cm")),
            step_limit_cm=_optional_float(value.get("step_limit_cm")),
            payload_kg=_optional_float(value.get("payload_kg")),
            success_rate=float(value.get("success_rate", 0.0)),
            card_version=int(value.get("card_version", 0)),
            state_updated_at=float(value.get("state_updated_at", 0.0)),
            view_version=int(value.get("view_version", 0)),
        )


@dataclass
class ExecutionAvailability:
    device_id: str
    verb: str
    available: bool
    checked_at: float


@dataclass
class BidMetrics:
    capability_match: bool
    online: bool
    healthy: bool
    idle: bool
    battery_margin: float
    endurance_margin_s: float
    space_constraints_satisfied: bool
    execution_available: bool
    distance_m: Optional[float]
    reliability: float

    @property
    def eligible(self) -> bool:
        return (
            self.capability_match
            and self.online
            and self.healthy
            and self.idle
            and self.battery_margin >= 0
            and self.endurance_margin_s >= 0
            and self.space_constraints_satisfied
            and self.execution_available
        )


@dataclass
class BidOffer:
    slot_id: str
    decision: str
    metrics: BidMetrics
    reason_codes: list[str] = field(default_factory=list)


@dataclass
class CollaborationProposal:
    preferred_slot_ids: list[str]
    cooperation_summary: str
    risk_notes: list[str]
    evidence_refs: list[str]
    confidence: float
    context_fingerprint: str


@dataclass
class BidPayload:
    task_id: str
    task_revision: int
    coordination_epoch: int
    bid_round: int
    device_id: str
    offers: list[BidOffer]
    proposal: Optional[CollaborationProposal]
    expires_at: float
    schema_version: int = SCHEMA_VERSION


@dataclass
class BidRecord:
    event_id: str
    event_version: int
    event_ts: float
    payload: BidPayload


@dataclass
class AssignmentSnapshot:
    assignment_id: str
    slot_id: str
    device_id: str
    supporting_bid_event_id: str
    assignment_epoch: int
    allowed_actions: list[str]
    action_template: dict[str, Any]
    completion_rule: dict[str, Any]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AssignmentSnapshot":
        return cls(
            assignment_id=str(value.get("assignment_id", "")),
            slot_id=str(value.get("slot_id", "")),
            device_id=str(value.get("device_id", "")),
            supporting_bid_event_id=str(value.get("supporting_bid_event_id", "")),
            assignment_epoch=int(value.get("assignment_epoch", 0)),
            allowed_actions=[enum_value(item) for item in value.get("allowed_actions", [])],
            action_template=dict(value.get("action_template", {})),
            completion_rule=dict(value.get("completion_rule", {})),
        )


@dataclass
class AssignmentPlanSnapshot:
    task_id: str
    task_revision: int
    coordination_epoch: int
    plan_id: str
    assignments: list[AssignmentSnapshot]
    committed_by: str
    input_evidence_refs: list[str]
    input_fingerprint: str
    schema_version: int = SCHEMA_VERSION


@dataclass
class CandidateAssignment:
    slot_id: str
    device_id: str
    supporting_bid_event_id: str


@dataclass
class CandidateAssignmentPlan:
    task_id: str
    task_revision: int
    coordination_epoch: int
    assignments: list[CandidateAssignment]
    rationale_summary: str
    input_fingerprint: str


@dataclass
class GroupPlanningInput:
    task: dict[str, Any]
    role_slots: list[RoleSlot]
    bids: list[BidRecord]
    proposals: list[CollaborationProposal]
    agent_snapshots: list[AgentPublicSnapshot]
    evidence_view: Optional[dict[str, Any]]
    task_revision: int
    coordination_epoch: int
    input_fingerprint: str


@dataclass
class Violation:
    code: str
    message: str
    slot_id: Optional[str] = None
    device_id: Optional[str] = None


@dataclass
class ValidPlan:
    assignment_plan: AssignmentPlanSnapshot


@dataclass
class InvalidPlan:
    violations: list[Violation]


PlanValidationResult = Union[ValidPlan, InvalidPlan]


@dataclass
class ActionCandidate:
    verb: str
    params: dict[str, Any]
    reversible: bool
    expected_observation: dict[str, Any] = field(default_factory=dict)
    reason_codes: list[str] = field(default_factory=list)


@dataclass
class SafetyVerdict:
    allowed: bool
    risk_level: RiskLevel
    needs_auth: bool
    reason_code: str
    reason: str = ""


@dataclass
class SkillQuery:
    task_type: str
    situation_tags: list[str] = field(default_factory=list)
    capability_ids: list[str] = field(default_factory=list)


@dataclass
class SkillReference:
    skill_id: str
    version: str
    title: str
    strategy_summary: str
    applicable_conditions: list[str]
    source_ref: str


@dataclass
class IntentSession:
    intent_id: str
    state: IntentState
    intent_fingerprint: str
    receipt_id: Optional[str] = None


@dataclass
class TaskSession:
    task_id: str
    task_revision: int
    coordination_epoch: int
    state: TaskSessionState
    assignment: Optional[AssignmentSnapshot]
    current_intent: Optional[IntentSession]
    last_receipt_id: Optional[str]
    session_version: int

    @classmethod
    def observed(cls, task_id: str, task_revision: int = 1) -> "TaskSession":
        return cls(
            task_id=task_id,
            task_revision=task_revision,
            coordination_epoch=0,
            state=TaskSessionState.OBSERVED,
            assignment=None,
            current_intent=None,
            last_receipt_id=None,
            session_version=1,
        )


@dataclass
class SubmitActionIntentPayload:
    intent: ActionIntent
    task_revision: int
    coordination_epoch: int
    plan_id: str
    assignment_id: str
    assignment_epoch: int
    step: int
    attempt: int
    intent_fingerprint: str


@dataclass
class LoopInput:
    event: BlackboardEvent
    offset: int
    now: float
    session: TaskSession
    task_view: Optional[dict[str, Any]]
    agent_snapshot: AgentPublicSnapshot
    evidence_view: Optional[dict[str, Any]]
    execution_availability: list[ExecutionAvailability]
    skill_references: list[SkillReference]
    local_proposal: Optional[CollaborationProposal]


@dataclass
class Effect:
    kind: EffectKind
    payload: Any = None


@dataclass
class StepResult:
    effects: list[Effect]
    session_after: TaskSession
    consume_input: bool


@dataclass
class ActionExecutionResult:
    intent_event: BlackboardEvent
    safety_intercept_event: Optional[BlackboardEvent]
    receipt_event: Optional[BlackboardEvent]
    append_results: list[AppendResult]


def enum_value(value: Any) -> str:
    """Return a stable string for an Enum-or-string value."""
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def event_type_value(event_or_type: Any) -> str:
    value = getattr(event_or_type, "type", event_or_type)
    return enum_value(value)


def event_content(event: BlackboardEvent) -> dict[str, Any]:
    value = getattr(event, "content", None)
    return value if isinstance(value, dict) else {}


def to_json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: to_json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_json_value(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        to_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def fingerprint(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def deterministic_event_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"event:{digest}"


def make_blackboard_event(
    event_type: Union[CoordinationEventType, EventType, str],
    ledger: Union[Ledger, str],
    content: Any,
    source: str,
    idempotency_key: str,
    *,
    confidence: Optional[float] = None,
    ttl: Optional[float] = None,
    event_id: Optional[str] = None,
) -> BlackboardEvent:
    type_value = enum_value(event_type)
    try:
        resolved_type: Any = EventType(type_value)
    except ValueError:
        resolved_type = type_value
    resolved_ledger = ledger if isinstance(ledger, Ledger) else Ledger(str(ledger))
    return BlackboardEvent(
        id=event_id or deterministic_event_id(idempotency_key),
        type=resolved_type,
        ledger=resolved_ledger,
        content=to_json_value(content),
        source=source,
        confidence=confidence,
        ttl=ttl,
        idempotency_key=idempotency_key,
    )


def intent_context(payload: SubmitActionIntentPayload) -> dict[str, Any]:
    return {
        "task_revision": payload.task_revision,
        "coordination_epoch": payload.coordination_epoch,
        "plan_id": payload.plan_id,
        "assignment_id": payload.assignment_id,
        "assignment_epoch": payload.assignment_epoch,
        "step": payload.step,
        "attempt": payload.attempt,
        "intent_fingerprint": payload.intent_fingerprint,
    }


def receipt_from_unknown(intent: ActionIntent) -> ActionReceipt:
    return ActionReceipt(
        intent_id=intent.intent_id,
        device_id=intent.device_id,
        success=False,
        result={},
        failure_reason="DISPATCH_UNKNOWN",
        recovery_class="unrecoverable",
        partial_progress=None,
        duration_s=0.0,
        extra={"outcome_certainty": OutcomeCertainty.UNKNOWN.value},
    )


def _optional_float(value: Any) -> Optional[float]:
    return None if value is None else float(value)
