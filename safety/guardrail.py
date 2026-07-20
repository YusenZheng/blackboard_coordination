# STATUS: STAGED(A类)—— MVP:三层贯通 hook 管线(语义/群体默认放行占位,物理门控走可逆性)
"""safety.guardrail —— pre/post-tool hook 管线,三层贯通。

实现 contracts.interfaces.SafetyPort。动作放行须同时过三层:
    ① 语义合规(MVP 占位默认放行)—— TODO:守护模型判指令安全性 + 受限解码。
    ② 群体安全(MVP 占位默认放行)—— TODO:防碰撞/激化/信息不一致/资源死锁四类多机风险。
    ③ 物理门控(已接 reversibility)—— R0 放行 / R1 需授权点 / R2 拦截。

【三处旁路不经此管线】高频遥测、急停、授权点回调走 contracts.bypass。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..contracts.task import ActionIntent, ActionReceipt
from .reversibility import Reversibility, classify


@dataclass
class Verdict:
    """安全裁决(实现 SafetyVerdict 形状)。"""
    allowed: bool
    needs_auth: bool = False
    reason: str = ""
    reversibility: str = "R0"


class Guardrail:
    """三层贯通的安全管线。"""

    def check(self, intent: ActionIntent, context: dict = None) -> Verdict:
        """pre-tool:同时过三层。"""
        context = context or {}
        # ① 语义合规(MVP 占位)
        # TODO:守护模型 + 受限解码,把"不追/不围/禁入区"编进生成
        # TODO:autonomy = policy_for(...);
        #   若动作风险高于任务声明的信任等级 → 降级 / 触发授权点(把 autonomy 串进放行决策)
        # ② 群体安全(MVP 占位)
        # TODO:碰撞锁 / 变化密度约束 / 强一致广播 / 死锁仲裁
        # ③ 物理门控(已实现:可逆性)
        rev = classify(intent, context)
        if rev == Reversibility.R2:
            return Verdict(allowed=False, reason="R2 禁止(禁区/越界)", reversibility="R2")
        if rev == Reversibility.R1:
            return Verdict(allowed=False, needs_auth=True,
                           reason="R1 不可逆,需授权点背书", reversibility="R1")
        return Verdict(allowed=True, reason="R0 可逆,三层通过", reversibility="R0")

    def post_check(self, receipt: ActionReceipt, context: dict = None) -> Verdict:
        """post-tool:结果合规性(MVP 默认通过)。"""
        return Verdict(allowed=True, reason="post-check 通过")
