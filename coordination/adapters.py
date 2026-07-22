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


class NullLocalProposalPolicy:
    def propose(self, context: dict, timeout_s: float) -> Optional[CollaborationProposal]:
        return None
