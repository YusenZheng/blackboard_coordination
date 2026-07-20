# STATUS: STAGED(A类)—— MVP 可跑:R0/R1/R2 判定 + R1 授权点挂起(串进 skeleton 链路二)
"""safety.reversibility —— 动作可逆性门控 R0/R1/R2。

【与信任 A0-A4 错开命名】这里是"某个具体动作能不能自主做",pre-tool hook。
    R0 可逆自主:移动/悬停/扫描 —— 直接放行。
    R1 不可逆需授权点:钻封闭缝隙/起降/抓取移动目标 —— 挂起 loop 等人工确认(走人类确认账本)。
    R2 禁止:越界/进禁区 —— 直接拦截。
【合成规则】可逆性门控优先于信任等级:无论 A3/A4,R1 动作仍强制走授权点。
示例场景之一:如"机器人钻封闭缝隙"这类不可逆动作触发授权点(具体判定表后续细化)。

TODO:真授权点接指挥官 App;R2 禁区表接地理围栏;可逆性判定接动作语义库。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from ..contracts.bypass import AuthDecision
from ..contracts.task import ActionIntent
from ..contracts.verbs import ActionVerb


class Reversibility(str, Enum):
    R0 = "R0"   # 可逆,自主放行
    R1 = "R1"   # 不可逆,需授权点
    R2 = "R2"   # 禁止


# MVP 判定表(够 skeleton 用;真上接动作语义库 + 场景上下文)
_IRREVERSIBLE_VERBS = {ActionVerb.PICK_PLACE}   # 抓取移动目标类
_REVERSIBLE_VERBS = {ActionVerb.MOVE_TO, ActionVerb.HOVER, ActionVerb.FOLLOW,
                     ActionVerb.RETURN_HOME, ActionVerb.STOP, ActionVerb.FORMATION}


def classify(intent: ActionIntent, context: Optional[dict] = None) -> Reversibility:
    """判定动作可逆性等级。

    MVP 规则:
        - intent.reversible=False,或进入 params 标了 enclosed(封闭缝隙)→ R1。
        - 进禁区(context 标 forbidden)→ R2。
        - 否则 R0。
    """
    context = context or {}
    if context.get("in_forbidden_zone"):
        return Reversibility.R2
    if intent.reversible is False or intent.params.get("enclosed"):
        return Reversibility.R1
    if intent.verb in _IRREVERSIBLE_VERBS:
        return Reversibility.R1
    return Reversibility.R0


class MockAuthorizationGate:
    """MVP 授权点:R1 动作挂起 → mock 确认(默认 5 秒无否决放行)。

    实现 contracts.bypass.AuthorizationGate 的形状。真上接指挥官 App + 人类确认账本。
    """
    def __init__(self, blackboard=None, trace=None, auto_approve: bool = True):
        self._bb = blackboard
        self._trace = trace
        self._auto = auto_approve
        self._pending: dict = {}

    def suspend(self, intent_id: str, context: dict) -> None:
        """挂起动作,向人类确认账本写 AUTH_POINT 事件,推指挥官。"""
        self._pending[intent_id] = context
        if self._bb is not None:
            from ..contracts.blackboard_event import BlackboardEvent, EventType, Ledger
            self._bb.append(BlackboardEvent(
                id=f"auth-{intent_id}", type=EventType.AUTH_POINT, ledger=Ledger.HUMAN,
                content={"intent_id": intent_id, "context": context}, source="safety"))
        print(f"  [授权点] 不可逆动作 {intent_id} 挂起,推指挥官确认…(R1)")

    def resume(self, intent_id: str, decision: AuthDecision) -> None:
        """人工决定回来,写 AUTH_DECISION 事件,放行/拒绝,记入 Trace。"""
        self._pending.pop(intent_id, None)
        if self._bb is not None:
            from ..contracts.blackboard_event import BlackboardEvent, EventType, Ledger
            self._bb.append(BlackboardEvent(
                id=f"authd-{intent_id}", type=EventType.AUTH_DECISION, ledger=Ledger.HUMAN,
                content={"intent_id": intent_id, "approved": decision.approved,
                         "approver": decision.approver}, source="human"))
        verdict = "放行" if decision.approved else "拒绝"
        print(f"  [授权点] {intent_id} 指挥官{verdict}(approver={decision.approver})")

    def auto_confirm(self, intent_id: str, approver: str = "commander") -> AuthDecision:
        """MVP:模拟指挥官确认(auto_approve 时放行)。"""
        d = AuthDecision(intent_id=intent_id, approved=self._auto, approver=approver,
                         reason="mock 5s no-veto" if self._auto else "mock veto")
        self.resume(intent_id, d)
        return d
