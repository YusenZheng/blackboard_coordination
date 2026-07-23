# STATUS: STAGED(A类)—— 协同端口的 Null/Mock 实现，不包含真实 Tool/Skill 逻辑
"""用于未接入外部层时的显式 Null 和确定性测试替身。"""
from __future__ import annotations

from typing import Callable, Optional

from ..contracts.task import ActionIntent, ActionReceipt
from .models import CollaborationProposal, RiskLevel, SafetyVerdict, SkillQuery, SkillReference


class NullSkillReferenceProvider:
    def search(self, query: SkillQuery, limit: int = 3) -> list[SkillReference]:
        return []


class NullPhysicalActionGateway:
    def is_available(self, device_id: str, verb: str) -> bool:
        return False

    def dispatch(self, intent: ActionIntent) -> ActionReceipt:
        return ActionReceipt(
            intent_id=intent.intent_id,
            device_id=intent.device_id,
            success=False,
            result={},
            failure_reason="EXECUTION_CHANNEL_UNAVAILABLE",
            recovery_class="unrecoverable",
            duration_s=0.0,
            extra={"outcome_certainty": "confirmed"},
        )


class MockPhysicalActionGateway:
    def __init__(
        self,
        *,
        available: bool = True,
        receipt_factory: Optional[Callable[[ActionIntent], ActionReceipt]] = None,
    ) -> None:
        self.available = available
        self.receipt_factory = receipt_factory
        self.dispatched_intent_ids: list[str] = []

    def is_available(self, device_id: str, verb: str) -> bool:
        return self.available

    def dispatch(self, intent: ActionIntent) -> ActionReceipt:
        self.dispatched_intent_ids.append(intent.intent_id)
        if self.receipt_factory is not None:
            return self.receipt_factory(intent)
        return ActionReceipt(
            intent_id=intent.intent_id,
            device_id=intent.device_id,
            success=True,
            result={"mock": True},
            duration_s=0.0,
            extra={"outcome_certainty": "confirmed"},
        )


class StaticSafetyPort:
    def __init__(
        self,
        pre: Optional[SafetyVerdict] = None,
        post: Optional[SafetyVerdict] = None,
    ) -> None:
        self.pre = pre or SafetyVerdict(True, RiskLevel.R0, False, "SAFETY_OK")
        self.post = post or SafetyVerdict(True, RiskLevel.R0, False, "POST_CHECK_OK")

    def pre_check(self, intent: ActionIntent) -> SafetyVerdict:
        return self.pre

    def post_check(self, intent: ActionIntent, receipt: ActionReceipt) -> SafetyVerdict:
        return self.post


class GuardrailSafetyPort:
    """Adapt the Harness Guardrail to the coordination-v2 SafetyPort contract."""

    def __init__(self, guardrail) -> None:
        self.guardrail = guardrail

    def pre_check(self, intent: ActionIntent) -> SafetyVerdict:
        verdict = self.guardrail.check(intent)
        risk_level = RiskLevel(str(verdict.reversibility))
        reason_code = {
            RiskLevel.R0: "SAFETY_OK",
            RiskLevel.R1: "AUTH_REQUIRED",
            RiskLevel.R2: "SAFETY_BLOCKED",
        }[risk_level]
        return SafetyVerdict(
            allowed=bool(verdict.allowed),
            risk_level=risk_level,
            needs_auth=bool(verdict.needs_auth),
            reason_code=reason_code,
            reason=str(verdict.reason),
        )

    def post_check(
        self, intent: ActionIntent, receipt: ActionReceipt
    ) -> SafetyVerdict:
        verdict = self.guardrail.post_check(receipt)
        return SafetyVerdict(
            allowed=bool(verdict.allowed),
            risk_level=RiskLevel.R0,
            needs_auth=False,
            reason_code="POST_CHECK_OK",
            reason=str(verdict.reason),
        )


class NullLocalProposalPolicy:
    def propose(self, context: dict, timeout_s: float) -> Optional[CollaborationProposal]:
        return None
