# STATUS: STAGED(A类)—— 协同层依赖端口；实现由 runtime/其他层注入
"""协同层所需的窄接口。

接口放在 coordination 内是当前 contracts v1 的兼容措施。它们只描述依赖，不实现
Blackboard、Safety、Access、LLM 或 Assets 的业务。
"""
from __future__ import annotations

from typing import Optional, Protocol

from ..contracts.blackboard_event import BlackboardEvent
from ..contracts.task import ActionIntent, ActionReceipt
from .models import (
    AppendResult,
    CandidateAssignmentPlan,
    CollaborationProposal,
    EventEnvelope,
    GroupPlanningInput,
    LoopInput,
    SafetyVerdict,
    SkillQuery,
    SkillReference,
    StepResult,
)


class BlackboardClientPort(Protocol):
    def now(self) -> float: ...

    def high_watermark(self) -> int: ...

    def append(self, event: BlackboardEvent) -> AppendResult: ...

    def read_since(
        self,
        offset: int,
        event_types: Optional[set[str]],
        limit: int,
        timeout_s: float,
    ) -> list[EventEnvelope]: ...

    def query_view(
        self,
        ledger: Optional[str],
        filt: dict,
        min_version: Optional[int],
        timeout_s: float,
    ) -> dict | list[dict]: ...


class PureAgentLoopPort(Protocol):
    def step(self, loop_input: LoopInput) -> StepResult: ...


class LocalProposalPolicyPort(Protocol):
    def propose(self, context: dict, timeout_s: float) -> Optional[CollaborationProposal]: ...


class GroupPlanningPolicyPort(Protocol):
    def plan(self, planning_input: GroupPlanningInput, timeout_s: float) -> CandidateAssignmentPlan: ...


class SafetyPort(Protocol):
    def pre_check(self, intent: ActionIntent) -> SafetyVerdict: ...

    def post_check(self, intent: ActionIntent, receipt: ActionReceipt) -> SafetyVerdict: ...


class PhysicalActionGatewayPort(Protocol):
    def is_available(self, device_id: str, verb: str) -> bool: ...

    def dispatch(self, intent: ActionIntent) -> ActionReceipt: ...


class SkillReferencePort(Protocol):
    def search(self, query: SkillQuery, limit: int = 3) -> list[SkillReference]: ...
