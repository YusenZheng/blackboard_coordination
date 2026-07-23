"""Validation rules for events accepted by the in-memory Blackboard.

The repository still runs a v1 walking skeleton whose compact events do not
carry ``schema_version``.  Those events remain supported.  Events declaring
``schema_version == 2`` are validated against the fields consumed by the
current coordination layer so an unparseable event cannot poison replayable
views.
"""
from __future__ import annotations

import math
from enum import Enum
from typing import Any, Callable, Optional

from ..contracts.blackboard_event import EventType


_MISSING = object()
_KNOWN_EVENT_TYPES = {item.value for item in EventType}
_V2_ONLY_EVENT_TYPES = {
    EventType.BID_ROUND_OPENED.value,
    EventType.ASSIGNMENT_COMPLETED.value,
    EventType.TASK_FAILED.value,
    EventType.ACTION_INTENT.value,
}
_V2_TASK_SCOPED_TYPES = {
    EventType.TASK_POSTED.value,
    EventType.BID_ROUND_OPENED.value,
    EventType.BID.value,
    EventType.TASK_ASSIGNED.value,
    EventType.ASSIGNMENT_COMPLETED.value,
    EventType.TASK_REPLAN.value,
    EventType.TASK_DONE.value,
    EventType.TASK_FAILED.value,
    EventType.CLUE.value,
    EventType.ACTION_INTENT.value,
    EventType.RECEIPT.value,
    EventType.SAFETY_INTERCEPT.value,
}
_V2_MARKER_FIELDS = {
    "task_revision",
    "coordination_epoch",
    "bid_round",
    "role_slots",
    "slots",
    "offers",
    "assignments",
    "plan_id",
    "assignment_id",
    "assignment_epoch",
    "completed_assignment_ids",
    "evidence_refs",
    "receipt_id",
    "from_epoch",
    "to_epoch",
    "intent_fingerprint",
    "outcome_certainty",
    "signal_id",
    "operation",
    "scope_type",
}


def enum_value(value: Any) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def is_v2_content(content: Any) -> bool:
    return isinstance(content, dict) and content.get("schema_version") == 2


def validate_event_contract(event_type: Any, content: dict[str, Any]) -> Optional[str]:
    """Return a stable rejection code, or ``None`` when the payload is safe."""

    type_value = enum_value(event_type)
    if type_value not in _KNOWN_EVENT_TYPES:
        return "EVENT_TYPE_UNKNOWN"

    common_error = _validate_present_common_fields(content)
    if common_error is not None:
        return common_error

    schema_version = content.get("schema_version", _MISSING)
    if schema_version is _MISSING:
        if type_value in _V2_ONLY_EVENT_TYPES or any(
            field_name in content for field_name in _V2_MARKER_FIELDS
        ):
            return "SCHEMA_VERSION_REQUIRED"
        return None
    if not _is_int(schema_version):
        return "SCHEMA_VERSION_INVALID"
    if schema_version != 2:
        return "SCHEMA_VERSION_UNSUPPORTED"

    if type_value in _V2_TASK_SCOPED_TYPES:
        error = _required_nonempty_string(content, "task_id")
        if error is not None:
            return error
        error = _required_int(content, "task_revision", minimum=1)
        if error is not None:
            return error

    validator = _V2_VALIDATORS.get(type_value)
    return validator(content) if validator is not None else None


def _validate_present_common_fields(content: dict[str, Any]) -> Optional[str]:
    if "task_id" in content and not _is_nonempty_string(content["task_id"]):
        return "TASK_ID_INVALID"
    integer_fields = {
        "task_revision": 1,
        "coordination_epoch": 0,
        "assignment_epoch": 1,
        "bid_round": 1,
        "step": 0,
        "attempt": 1,
        "from_epoch": 0,
        "to_epoch": 1,
    }
    for field_name, minimum in integer_fields.items():
        if field_name not in content:
            continue
        value = content[field_name]
        if not _is_int(value):
            return f"{field_name.upper()}_TYPE_INVALID"
        if value < minimum:
            return f"{field_name.upper()}_OUT_OF_RANGE"

    number_fields = {
        "deadline": 0.0,
        "expires_at": 0.0,
        "completed_at": 0.0,
        "failed_at": 0.0,
        "duration_s": 0.0,
        "final_progress": 0.0,
    }
    for field_name, minimum in number_fields.items():
        if field_name not in content:
            continue
        value = content[field_name]
        if not _is_finite_number(value):
            return f"{field_name.upper()}_TYPE_INVALID"
        if minimum is not None and float(value) < minimum:
            return f"{field_name.upper()}_OUT_OF_RANGE"
    return None


def _validate_task_posted(content: dict[str, Any]) -> Optional[str]:
    if "role_slots" not in content:
        return None
    if not isinstance(content["role_slots"], list):
        return "ROLE_SLOTS_TYPE_INVALID"
    if "requirement" in content and not isinstance(content["requirement"], dict):
        return "REQUIREMENT_TYPE_INVALID"
    for slot in content["role_slots"]:
        if not isinstance(slot, dict):
            return "ROLE_SLOT_TYPE_INVALID"
        error = _required_nonempty_string(slot, "slot_id")
        if error is not None:
            return "ROLE_SLOT_ID_REQUIRED"
        for field_name in ("required_capability_ids", "allowed_actions"):
            if field_name in slot and not _is_string_list(slot[field_name]):
                return f"ROLE_SLOT_{field_name.upper()}_INVALID"
        for field_name in ("action_template", "completion_rule"):
            if field_name in slot and not isinstance(slot[field_name], dict):
                return f"ROLE_SLOT_{field_name.upper()}_INVALID"
    return None


def _validate_bid_round(content: dict[str, Any]) -> Optional[str]:
    for field_name, minimum in (
        ("coordination_epoch", 1),
        ("bid_round", 1),
    ):
        error = _required_int(content, field_name, minimum=minimum)
        if error is not None:
            return error
    if not _is_string_list(content.get("slots")):
        return "SLOTS_TYPE_INVALID"
    return _required_number(content, "deadline")


_BID_METRIC_BOOL_FIELDS = {
    "capability_match",
    "online",
    "healthy",
    "idle",
    "space_constraints_satisfied",
    "execution_available",
}
_BID_METRIC_NUMBER_FIELDS = {
    "battery_margin",
    "endurance_margin_s",
    "reliability",
}
_BID_METRIC_FIELDS = (
    _BID_METRIC_BOOL_FIELDS
    | _BID_METRIC_NUMBER_FIELDS
    | {"distance_m"}
)


def _validate_bid(content: dict[str, Any]) -> Optional[str]:
    for field_name in ("coordination_epoch", "bid_round"):
        error = _required_int(content, field_name, minimum=1)
        if error is not None:
            return error
    error = _required_nonempty_string(content, "device_id")
    if error is not None:
        return error
    error = _required_list(content, "offers")
    if error is not None:
        return error
    error = _required_number(content, "expires_at")
    if error is not None:
        return error

    for offer in content["offers"]:
        if not isinstance(offer, dict):
            return "BID_OFFER_TYPE_INVALID"
        if _required_nonempty_string(offer, "slot_id") is not None:
            return "BID_OFFER_SLOT_ID_REQUIRED"
        if offer.get("decision") not in {"accept", "reject"}:
            return "BID_OFFER_DECISION_INVALID"
        metrics = offer.get("metrics")
        if not isinstance(metrics, dict) or set(metrics) != _BID_METRIC_FIELDS:
            return "BID_METRICS_FIELDS_INVALID"
        if any(type(metrics[name]) is not bool for name in _BID_METRIC_BOOL_FIELDS):
            return "BID_METRICS_BOOL_INVALID"
        if any(
            not _is_finite_number(metrics[name])
            for name in _BID_METRIC_NUMBER_FIELDS
        ):
            return "BID_METRICS_NUMBER_INVALID"
        distance = metrics["distance_m"]
        if distance is not None and not _is_finite_number(distance):
            return "BID_DISTANCE_INVALID"
        reliability = float(metrics["reliability"])
        if not 0.0 <= reliability <= 1.0:
            return "BID_RELIABILITY_OUT_OF_RANGE"
        if "reason_codes" in offer and not _is_string_list(offer["reason_codes"]):
            return "BID_REASON_CODES_INVALID"

    proposal = content.get("proposal")
    if proposal is not None:
        error = _validate_proposal(proposal)
        if error is not None:
            return error
    if "skill_references" in content:
        error = _validate_skill_references(content["skill_references"])
        if error is not None:
            return error
    return None


def _validate_proposal(proposal: Any) -> Optional[str]:
    expected = {
        "preferred_slot_ids",
        "cooperation_summary",
        "risk_notes",
        "evidence_refs",
        "confidence",
        "context_fingerprint",
    }
    if not isinstance(proposal, dict) or set(proposal) != expected:
        return "BID_PROPOSAL_FIELDS_INVALID"
    for field_name in ("preferred_slot_ids", "risk_notes", "evidence_refs"):
        if not _is_string_list(proposal[field_name]):
            return "BID_PROPOSAL_LIST_INVALID"
    for field_name in ("cooperation_summary", "context_fingerprint"):
        if not isinstance(proposal[field_name], str):
            return "BID_PROPOSAL_STRING_INVALID"
    if not _is_finite_number(proposal["confidence"]):
        return "BID_PROPOSAL_CONFIDENCE_INVALID"
    if not 0.0 <= float(proposal["confidence"]) <= 1.0:
        return "BID_PROPOSAL_CONFIDENCE_OUT_OF_RANGE"
    return None


def _validate_skill_references(value: Any) -> Optional[str]:
    if not isinstance(value, list):
        return "SKILL_REFERENCES_TYPE_INVALID"
    expected = {
        "skill_id",
        "version",
        "title",
        "strategy_summary",
        "applicable_conditions",
        "source_ref",
        "tool_chain",
    }
    identities: set[tuple[str, str]] = set()
    for reference in value:
        if not isinstance(reference, dict) or set(reference) != expected:
            return "SKILL_REFERENCE_FIELDS_INVALID"
        for field_name in (
            "skill_id",
            "version",
            "title",
            "strategy_summary",
            "source_ref",
        ):
            if not isinstance(reference[field_name], str):
                return "SKILL_REFERENCE_STRING_INVALID"
        if not reference["skill_id"] or not reference["version"] or not reference["source_ref"]:
            return "SKILL_REFERENCE_IDENTITY_REQUIRED"
        for field_name in ("applicable_conditions", "tool_chain"):
            if not _is_string_list(reference[field_name]):
                return "SKILL_REFERENCE_LIST_INVALID"
        identity = (reference["skill_id"], reference["version"])
        if identity in identities:
            return "SKILL_REFERENCE_DUPLICATE"
        identities.add(identity)
    return None


def _validate_task_assigned(content: dict[str, Any]) -> Optional[str]:
    error = _required_int(content, "coordination_epoch", minimum=1)
    if error is not None:
        return error
    error = _required_nonempty_string(content, "plan_id")
    if error is not None:
        return error
    error = _required_list(content, "assignments")
    if error is not None:
        return error
    for assignment in content["assignments"]:
        error = _validate_assignment(assignment)
        if error is not None:
            return error
        if assignment["assignment_epoch"] != content["coordination_epoch"]:
            return "EPOCH_RELATION_INVALID"
    for field_name in ("committed_by", "input_fingerprint"):
        if field_name in content and not isinstance(content[field_name], str):
            return f"{field_name.upper()}_TYPE_INVALID"
    if "input_evidence_refs" in content and not _is_string_list(
        content["input_evidence_refs"]
    ):
        return "INPUT_EVIDENCE_REFS_INVALID"
    return None


def _validate_assignment(assignment: Any) -> Optional[str]:
    if not isinstance(assignment, dict):
        return "ASSIGNMENT_TYPE_INVALID"
    for field_name in (
        "assignment_id",
        "slot_id",
        "device_id",
        "supporting_bid_event_id",
    ):
        if _required_nonempty_string(assignment, field_name) is not None:
            return f"ASSIGNMENT_{field_name.upper()}_REQUIRED"
    if _required_int(assignment, "assignment_epoch", minimum=1) is not None:
        return "ASSIGNMENT_EPOCH_INVALID"
    if not _is_string_list(assignment.get("allowed_actions")):
        return "ASSIGNMENT_ALLOWED_ACTIONS_INVALID"
    for field_name in ("action_template", "completion_rule"):
        if not isinstance(assignment.get(field_name), dict):
            return f"ASSIGNMENT_{field_name.upper()}_INVALID"
    if "skill_references" in assignment:
        error = _validate_skill_references(assignment["skill_references"])
        if error is not None:
            return error
    return None


def _validate_action_context(content: dict[str, Any]) -> Optional[str]:
    error = _required_int(content, "coordination_epoch", minimum=1)
    if error is not None:
        return error
    error = _required_int(content, "assignment_epoch", minimum=1)
    if error is not None:
        return error
    for field_name in ("plan_id", "assignment_id", "intent_id", "device_id"):
        error = _required_nonempty_string(content, field_name)
        if error is not None:
            return error
    if content["assignment_epoch"] != content["coordination_epoch"]:
        return "EPOCH_RELATION_INVALID"
    return None


def _validate_action_intent(content: dict[str, Any]) -> Optional[str]:
    error = _validate_action_context(content)
    if error is not None:
        return error
    for field_name in ("verb", "intent_fingerprint"):
        error = _required_nonempty_string(content, field_name)
        if error is not None:
            return error
    if not isinstance(content.get("params"), dict):
        return "PARAMS_TYPE_INVALID"
    if type(content.get("reversible")) is not bool:
        return "REVERSIBLE_TYPE_INVALID"
    if content.get("step") != 0 or type(content.get("step")) is not int:
        return "STEP_UNSUPPORTED"
    if content.get("attempt") != 1 or type(content.get("attempt")) is not int:
        return "ATTEMPT_UNSUPPORTED"
    if "skill_references" in content:
        error = _validate_skill_references(content["skill_references"])
        if error is not None:
            return error
    return None


def _validate_safety_intercept(content: dict[str, Any]) -> Optional[str]:
    error = _validate_action_context(content)
    if error is not None:
        return error
    if content.get("check_phase") not in {"pre", "post"}:
        return "CHECK_PHASE_INVALID"
    if content.get("risk_level") not in {"R0", "R1", "R2"}:
        return "RISK_LEVEL_INVALID"
    return _required_nonempty_string(content, "reason_code")


def _validate_receipt(content: dict[str, Any]) -> Optional[str]:
    error = _validate_action_context(content)
    if error is not None:
        return error
    error = _required_nonempty_string(content, "receipt_id")
    if error is not None:
        return error
    if _required_int(content, "attempt", minimum=1) is not None:
        return "ATTEMPT_INVALID"
    if type(content.get("success")) is not bool:
        return "SUCCESS_TYPE_INVALID"
    if content.get("outcome_certainty") not in {"confirmed", "unknown"}:
        return "OUTCOME_CERTAINTY_INVALID"
    if not isinstance(content.get("result"), dict):
        return "RESULT_TYPE_INVALID"
    error = _required_number(content, "duration_s", minimum=0.0)
    if error is not None:
        return error
    if type(content.get("post_check_allowed")) is not bool:
        return "POST_CHECK_ALLOWED_TYPE_INVALID"
    return None


def _validate_assignment_completed(content: dict[str, Any]) -> Optional[str]:
    for field_name in ("coordination_epoch", "assignment_epoch"):
        error = _required_int(content, field_name, minimum=1)
        if error is not None:
            return error
    for field_name in (
        "plan_id",
        "assignment_id",
        "device_id",
        "receipt_id",
    ):
        error = _required_nonempty_string(content, field_name)
        if error is not None:
            return error
    if content["assignment_epoch"] != content["coordination_epoch"]:
        return "EPOCH_RELATION_INVALID"
    if not _is_string_list(content.get("completion_evidence")):
        return "COMPLETION_EVIDENCE_INVALID"
    error = _required_number(content, "final_progress", minimum=0.0)
    if error is not None:
        return error
    if float(content["final_progress"]) > 1.0:
        return "FINAL_PROGRESS_OUT_OF_RANGE"
    return _required_number(content, "completed_at")


def _validate_clue(content: dict[str, Any]) -> Optional[str]:
    clue_id = content.get("clue_id")
    dedupe_key = content.get("dedupe_key")
    if not _is_nonempty_string(clue_id) and not _is_nonempty_string(dedupe_key):
        return "CLUE_ID_REQUIRED"
    if "expires_at" in content and not _is_finite_number(content["expires_at"]):
        return "EXPIRES_AT_TYPE_INVALID"
    return None


def _validate_task_replan(content: dict[str, Any]) -> Optional[str]:
    for field_name, minimum in (("from_epoch", 0), ("to_epoch", 1)):
        error = _required_int(content, field_name, minimum=minimum)
        if error is not None:
            return error
    if content["to_epoch"] != content["from_epoch"] + 1:
        return "EPOCH_RELATION_INVALID"
    error = _required_nonempty_string(content, "cause_event_id")
    if error is not None:
        return error
    if not _is_string_list(content.get("evidence_refs")):
        return "EVIDENCE_REFS_INVALID"
    return None


def _validate_estop(content: dict[str, Any]) -> Optional[str]:
    error = _required_nonempty_string(content, "signal_id")
    if error is not None:
        return error
    if content.get("operation") not in {"stop", "clear"}:
        return "ESTOP_OPERATION_INVALID"
    if content.get("scope_type") not in {"all", "device"}:
        return "ESTOP_SCOPE_TYPE_INVALID"
    if content["scope_type"] == "device" and not _is_nonempty_string(
        content.get("scope_id")
    ):
        return "ESTOP_SCOPE_ID_REQUIRED"
    if "task_id" in content and "task_revision" not in content:
        return "TASK_REVISION_REQUIRED"
    return None


def _validate_task_done(content: dict[str, Any]) -> Optional[str]:
    error = _required_int(content, "coordination_epoch", minimum=1)
    if error is not None:
        return error
    error = _required_nonempty_string(content, "plan_id")
    if error is not None:
        return error
    for field_name in ("completed_assignment_ids", "completion_evidence"):
        if not _is_string_list(content.get(field_name)):
            return f"{field_name.upper()}_INVALID"
    return _required_number(content, "completed_at")


def _validate_task_failed(content: dict[str, Any]) -> Optional[str]:
    error = _required_int(content, "coordination_epoch", minimum=0)
    if error is not None:
        return error
    for field_name in ("reason_code", "failed_component"):
        error = _required_nonempty_string(content, field_name)
        if error is not None:
            return error
    if not _is_string_list(content.get("related_event_ids")):
        return "RELATED_EVENT_IDS_INVALID"
    if type(content.get("retryable")) is not bool:
        return "RETRYABLE_TYPE_INVALID"
    return _required_number(content, "failed_at")


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _required_nonempty_string(
    content: dict[str, Any], field_name: str
) -> Optional[str]:
    if field_name not in content:
        return f"{field_name.upper()}_REQUIRED"
    if not _is_nonempty_string(content[field_name]):
        return f"{field_name.upper()}_INVALID"
    return None


def _required_int(
    content: dict[str, Any], field_name: str, *, minimum: int
) -> Optional[str]:
    if field_name not in content:
        return f"{field_name.upper()}_REQUIRED"
    value = content[field_name]
    if not _is_int(value):
        return f"{field_name.upper()}_TYPE_INVALID"
    if value < minimum:
        return f"{field_name.upper()}_OUT_OF_RANGE"
    return None


def _required_number(
    content: dict[str, Any], field_name: str, *, minimum: Optional[float] = None
) -> Optional[str]:
    if field_name not in content:
        return f"{field_name.upper()}_REQUIRED"
    value = content[field_name]
    if not _is_finite_number(value):
        return f"{field_name.upper()}_TYPE_INVALID"
    if minimum is not None and float(value) < minimum:
        return f"{field_name.upper()}_OUT_OF_RANGE"
    return None


def _required_list(content: dict[str, Any], field_name: str) -> Optional[str]:
    if field_name not in content:
        return f"{field_name.upper()}_REQUIRED"
    if not isinstance(content[field_name], list):
        return f"{field_name.upper()}_TYPE_INVALID"
    return None


_V2_VALIDATORS: dict[str, Callable[[dict[str, Any]], Optional[str]]] = {
    EventType.TASK_POSTED.value: _validate_task_posted,
    EventType.BID_ROUND_OPENED.value: _validate_bid_round,
    EventType.BID.value: _validate_bid,
    EventType.TASK_ASSIGNED.value: _validate_task_assigned,
    EventType.ACTION_INTENT.value: _validate_action_intent,
    EventType.SAFETY_INTERCEPT.value: _validate_safety_intercept,
    EventType.RECEIPT.value: _validate_receipt,
    EventType.ASSIGNMENT_COMPLETED.value: _validate_assignment_completed,
    EventType.CLUE.value: _validate_clue,
    EventType.TASK_REPLAN.value: _validate_task_replan,
    EventType.ESTOP.value: _validate_estop,
    EventType.TASK_DONE.value: _validate_task_done,
    EventType.TASK_FAILED.value: _validate_task_failed,
}
