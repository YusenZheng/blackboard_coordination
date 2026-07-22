# STATUS: STAGED(A类)—— ActionCandidate 编译及 Safety/Access 窄接口编排
"""动作意图执行边界。

PureAgentLoop 不调用本模块依赖的 I/O。Host 执行 submit_action_intent Effect 时才进入
这里。动作顺序固定为 ACTION_INTENT → Safety pre → dispatch → Safety post → RECEIPT。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from ..contracts.blackboard_event import BlackboardEvent, Ledger
from ..contracts.task import ActionIntent, ActionReceipt
from ..contracts.verbs import ActionVerb
from .models import (
    ActionCandidate,
    ActionExecutionResult,
    AppendResult,
    CoordinationEventType,
    OutcomeCertainty,
    RiskLevel,
    SafetyVerdict,
    SubmitActionIntentPayload,
    enum_value,
    fingerprint,
    intent_context,
    make_blackboard_event,
    receipt_from_unknown,
)
from .ports import PhysicalActionGatewayPort, SafetyPort


class DeterministicActionPolicy:
    def candidate(self, assignment) -> ActionCandidate:
        template = dict(assignment.action_template)
        verb = enum_value(template.get("verb", ""))
        if verb not in assignment.allowed_actions:
            raise ValueError("ACTION_NOT_ALLOWED")
        try:
            ActionVerb(verb)
        except ValueError as error:
            raise ValueError("UNKNOWN_ACTION_VERB") from error
        return ActionCandidate(
            verb=verb,
            params=dict(template.get("params", {})),
            reversible=bool(template.get("reversible", True)),
            expected_observation=dict(template.get("expected_observation", {})),
            reason_codes=["ASSIGNMENT_ACTION_TEMPLATE"],
        )

    def build_submit_payload(
        self,
        *,
        task_id: str,
        task_revision: int,
        coordination_epoch: int,
        plan_id: str,
        assignment,
    ) -> SubmitActionIntentPayload:
        candidate = self.candidate(assignment)
        step = 0
        attempt = 1
        intent_id = (
            f"intent:{task_id}:{task_revision}:{coordination_epoch}:"
            f"{assignment.assignment_id}:{assignment.assignment_epoch}:{step}:{attempt}"
        )
        base = {
            "intent_id": intent_id,
            "task_id": task_id,
            "task_revision": task_revision,
            "coordination_epoch": coordination_epoch,
            "plan_id": plan_id,
            "assignment_id": assignment.assignment_id,
            "assignment_epoch": assignment.assignment_epoch,
            "device_id": assignment.device_id,
            "verb": candidate.verb,
            "params": candidate.params,
            "reversible": candidate.reversible,
            "step": step,
            "attempt": attempt,
        }
        intent_fingerprint = fingerprint(base)
        intent = ActionIntent(
            intent_id=intent_id,
            device_id=assignment.device_id,
            verb=ActionVerb(candidate.verb),
            params=candidate.params,
            reversible=candidate.reversible,
            task_id=task_id,
            extra={
                "task_revision": task_revision,
                "coordination_epoch": coordination_epoch,
                "plan_id": plan_id,
                "assignment_id": assignment.assignment_id,
                "assignment_epoch": assignment.assignment_epoch,
                "step": step,
                "attempt": attempt,
                "intent_fingerprint": intent_fingerprint,
            },
        )
        return SubmitActionIntentPayload(
            intent=intent,
            task_revision=task_revision,
            coordination_epoch=coordination_epoch,
            plan_id=plan_id,
            assignment_id=assignment.assignment_id,
            assignment_epoch=assignment.assignment_epoch,
            step=step,
            attempt=attempt,
            intent_fingerprint=intent_fingerprint,
        )


class ActionExecutor:
    def __init__(
        self,
        *,
        safety: SafetyPort,
        gateway: PhysicalActionGatewayPort,
        publish_event: Optional[Callable[[BlackboardEvent], AppendResult]] = None,
        store_raw_receipt: Optional[Callable[[str, str, Any], None]] = None,
        remove_raw_receipt: Optional[Callable[[str, str], None]] = None,
        on_dispatching: Optional[Callable[[SubmitActionIntentPayload], None]] = None,
    ) -> None:
        self.safety = safety
        self.gateway = gateway
        self.publish_event = publish_event
        self.store_raw_receipt = store_raw_receipt
        self.remove_raw_receipt = remove_raw_receipt
        self.on_dispatching = on_dispatching

    def execute(self, payload: SubmitActionIntentPayload) -> ActionExecutionResult:
        self._validate_payload(payload)
        if self.publish_event is None:
            raise RuntimeError("ACTION_EVENT_PUBLISHER_NOT_CONFIGURED")
        intent_event = self._intent_event(payload)
        append_results = [self.publish_event(intent_event)]

        pre = self.safety.pre_check(payload.intent)
        self._validate_pre_verdict(pre)
        if not pre.allowed:
            intercept = self._intercept_event(payload, pre, "pre")
            append_results.append(self.publish_event(intercept))
            return ActionExecutionResult(intent_event, intercept, None, append_results)

        if self.on_dispatching is not None:
            self.on_dispatching(payload)

        dispatch_unknown = False
        try:
            receipt = self.gateway.dispatch(payload.intent)
            if receipt.intent_id != payload.intent.intent_id or receipt.device_id != payload.intent.device_id:
                dispatch_unknown = True
                receipt = receipt_from_unknown(payload.intent)
                receipt.failure_reason = "RECEIPT_CONTEXT_MISMATCH"
        except Exception:
            dispatch_unknown = True
            receipt = receipt_from_unknown(payload.intent)

        if self.store_raw_receipt is not None:
            self.store_raw_receipt(payload.intent.task_id or "", payload.intent.intent_id, receipt)

        if dispatch_unknown:
            post = SafetyVerdict(
                allowed=False,
                risk_level=RiskLevel.R2,
                needs_auth=False,
                reason_code="DISPATCH_UNKNOWN",
                reason="dispatch result is not authoritative",
            )
        else:
            post = self.safety.post_check(payload.intent, receipt)
            self._validate_post_verdict(post)

        intercept = None
        if not post.allowed:
            intercept = self._intercept_event(payload, post, "post")
            append_results.append(self.publish_event(intercept))

        receipt_event = self._receipt_event(payload, receipt, post, dispatch_unknown)
        append_results.append(self.publish_event(receipt_event))
        if self.remove_raw_receipt is not None:
            self.remove_raw_receipt(payload.intent.task_id or "", payload.intent.intent_id)
        return ActionExecutionResult(intent_event, intercept, receipt_event, append_results)

    @staticmethod
    def _validate_payload(payload: SubmitActionIntentPayload) -> None:
        if payload.step != 0 or payload.attempt != 1:
            raise ValueError("UNSUPPORTED_ACTION_SEQUENCE")
        expected = intent_context(payload)
        for key, value in expected.items():
            existing = payload.intent.extra.get(key)
            if existing is not None and existing != value:
                raise ValueError("INTENT_CONTEXT_MISMATCH")
            payload.intent.extra[key] = value
        if payload.intent.task_id is None:
            raise ValueError("INTENT_TASK_ID_REQUIRED")

    @staticmethod
    def _validate_pre_verdict(verdict: SafetyVerdict) -> None:
        level = RiskLevel(enum_value(verdict.risk_level))
        if level == RiskLevel.R0 and (not verdict.allowed or verdict.needs_auth):
            raise ValueError("INVALID_R0_VERDICT")
        if level == RiskLevel.R1 and (verdict.allowed or not verdict.needs_auth):
            raise ValueError("INVALID_R1_VERDICT")
        if level == RiskLevel.R2 and (verdict.allowed or verdict.needs_auth):
            raise ValueError("INVALID_R2_VERDICT")

    @staticmethod
    def _validate_post_verdict(verdict: SafetyVerdict) -> None:
        level = RiskLevel(enum_value(verdict.risk_level))
        if verdict.allowed and (level != RiskLevel.R0 or verdict.needs_auth):
            raise ValueError("INVALID_POST_VERDICT")

    @staticmethod
    def _intent_event(payload: SubmitActionIntentPayload) -> BlackboardEvent:
        intent = payload.intent
        content = {
            "schema_version": 2,
            "task_id": intent.task_id,
            "task_revision": payload.task_revision,
            "coordination_epoch": payload.coordination_epoch,
            "plan_id": payload.plan_id,
            "assignment_id": payload.assignment_id,
            "assignment_epoch": payload.assignment_epoch,
            "intent_id": intent.intent_id,
            "device_id": intent.device_id,
            "verb": enum_value(intent.verb),
            "params": dict(intent.params),
            "reversible": intent.reversible,
            "step": payload.step,
            "attempt": payload.attempt,
            "intent_fingerprint": payload.intent_fingerprint,
        }
        return make_blackboard_event(
            CoordinationEventType.ACTION_INTENT,
            Ledger.RECEIPT,
            content,
            intent.device_id,
            intent.intent_id,
        )

    @staticmethod
    def _intercept_event(
        payload: SubmitActionIntentPayload, verdict: SafetyVerdict, phase: str
    ) -> BlackboardEvent:
        intent = payload.intent
        content = {
            "schema_version": 2,
            "task_id": intent.task_id,
            "task_revision": payload.task_revision,
            "coordination_epoch": payload.coordination_epoch,
            "plan_id": payload.plan_id,
            "assignment_id": payload.assignment_id,
            "assignment_epoch": payload.assignment_epoch,
            "intent_id": intent.intent_id,
            "device_id": intent.device_id,
            "check_phase": phase,
            "risk_level": enum_value(verdict.risk_level),
            "reason_code": verdict.reason_code,
            "reason": verdict.reason,
        }
        return make_blackboard_event(
            CoordinationEventType.SAFETY_INTERCEPT,
            Ledger.RECEIPT,
            content,
            intent.device_id,
            f"safety-intercept:{intent.intent_id}:{phase}",
        )

    @staticmethod
    def _receipt_event(
        payload: SubmitActionIntentPayload,
        receipt: ActionReceipt,
        post: SafetyVerdict,
        dispatch_unknown: bool,
    ) -> BlackboardEvent:
        if dispatch_unknown:
            certainty = OutcomeCertainty.UNKNOWN.value
        else:
            certainty = enum_value(
                receipt.extra.get("outcome_certainty", OutcomeCertainty.CONFIRMED.value)
            )
        success = bool(receipt.success and certainty == OutcomeCertainty.CONFIRMED.value)
        content = {
            "schema_version": 2,
            "receipt_id": f"receipt:{payload.intent.intent_id}",
            "task_id": payload.intent.task_id,
            "task_revision": payload.task_revision,
            "coordination_epoch": payload.coordination_epoch,
            "plan_id": payload.plan_id,
            "assignment_id": payload.assignment_id,
            "assignment_epoch": payload.assignment_epoch,
            "intent_id": payload.intent.intent_id,
            "device_id": payload.intent.device_id,
            "attempt": payload.attempt,
            "success": success,
            "outcome_certainty": certainty,
            "result": dict(receipt.result),
            "failure_reason": receipt.failure_reason,
            "recovery_class": receipt.recovery_class,
            "partial_progress": receipt.partial_progress,
            "duration_s": max(0.0, float(receipt.duration_s)),
            "post_check_allowed": bool(post.allowed),
            "post_check_reason": None if post.allowed else post.reason_code,
        }
        return make_blackboard_event(
            CoordinationEventType.RECEIPT,
            Ledger.RECEIPT,
            content,
            payload.intent.device_id,
            f"receipt:{payload.intent.intent_id}",
        )
