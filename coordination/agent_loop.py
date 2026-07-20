# STATUS: STAGED(A类)—— MVP:极薄 loop,含 skill检索→(写死)决策→tool_call→回喂。真LLM推理是TODO
"""coordination.agent_loop —— 每台设备的云端虚拟 Agent 常驻循环。实现 AgentLoopPort。

极薄自研(不复用大框架)。标准 agentic loop 形状:
    装 context →(检索 Skill 作参考)→ LLM 推理 → tool_call → observation 回喂 → 再推理。
我们做了三处物理化(v5 §5.3):工具=契约动词;预算=电量+时间窗;记忆=履历。

【本轮补齐 skill 检索 + tool_call 两环节】此前 loop 只有"写死 bid",没有 agentic loop 的
tool_call/skill 检索。现在:
    - 决策前用 SkillGraph.match_by_situation 检索经验(命中作参考,用不用 loop 自己定 → 机制/策略分离)。
    - 应征后 loop 自己发起 tool_call(经 tool_gateway),而非 harness 代劳 —— 这才是真 agent loop。

【A9 诚实】MVP 的 bid/是否用 Skill 都是写死的,没有真 LLM 读黑板自主决策。真 LLM 涌现见 L1。

TODO:真 LLM 推理(读装配好的 context + Skill 参考 → 输出决策/动作意图);
    应征两段式(规则硬过滤已在 registry.discover;此处补模型软排序出 bid)。
"""
from __future__ import annotations

from ..contracts.blackboard_event import BlackboardEvent, EventType, Ledger
from ..contracts.task import ActionIntent
from ..contracts.types import ParticipationDecision
from .context_assembler import Budget, ContextAssembler


class AgentLoop:
    """一台设备的云端虚拟 Agent 替身的常驻循环。"""

    def __init__(self, device_card, blackboard, assembler: ContextAssembler = None,
                 skill_graph=None, tool_gateway=None, trace=None,
                 guardrail=None, auth_gate=None):
        self.card = device_card
        self.bb = blackboard
        self.assembler = assembler or ContextAssembler()
        self.skill_graph = skill_graph      # 检索经验参考用(可为 None)
        self.tool_gateway = tool_gateway    # loop 自己发起 tool_call 用(可为 None)
        self.trace = trace                  # B5:记模型级细节(可为 None)
        self.guardrail = guardrail          # B2:act 前强制安全校验(可为 None)
        self.auth_gate = auth_gate          # B2:R1 不可逆动作挂起授权点(可为 None)

    def on_event(self, event: BlackboardEvent, offset: int = 0) -> None:
        """黑板新事件驱动:任务发布 → 应征;新线索 → 重协同(共享黑板自组织)。"""
        if event.type == EventType.TASK_POSTED:
            self._maybe_bid(event.content.get("task_id"))
        elif event.type == EventType.CLUE:
            # 线索进黑板 → 本 loop 感知到共享状态变化(真上按线索决定是否改行为)
            self.on_replan(event.content.get("task_id"), event.content.get("clue_id"))

    def on_replan(self, task_id: str, clue_id: str) -> None:
        """B6:新线索驱动的就地重协同入口。

        MVP:读黑板最新状态、记一条重规划痕迹(真上:线索融合→判断是否改派/接力/蹲守)。
        这是"状态变了计划跟着变、不一条道跑到黑"的机制落点。
        """
        # TODO:基于线索融合结果决定重新 bid / 让位 / 接力 / 预置蹲守
        pass

    def step(self) -> None:
        """跑一轮(MVP 空转;真上是持续 sense-decide-act)。"""
        pass

    def _maybe_bid(self, task_id: str) -> None:
        """MVP:装 context →(检索 Skill 参考)→ 写死 bid 决策(真 LLM 推理是 TODO)。"""
        budget = Budget(battery=self.card.state.battery)
        ctx = self.assembler.assemble("要不要应征", self.bb, self.card, budget)

        # ── Skill 检索环节:命中已沉淀经验作参考(用不用 loop 自己定 → 机制/策略分离)──
        # 情境键从任务上下文推出(A1:不硬编码;MVP 用默认映射,"任务→情境→哪个Skill"的
        # 判定开口留给算法组在 L1 细化)。
        situation = self._situation_of(task_id)
        skill_hint = None
        if self.skill_graph is not None and situation:
            hit = self.skill_graph.match_by_situation(situation)
            if hit is not None:
                skill_hint = hit.signature
                ctx["skill_reference"] = {"signature": hit.signature, "strategy": hit.strategy}

        did = self.card.identity.device_id
        if self.card.state.battery < 0.2:
            decision = ParticipationDecision(device_id=did, decision="reject",
                                             reason_codes=["电量不足,拒绝(硬过滤)"])
        else:
            # MVP:bid 直接用电量(足够让 skeleton 分出胜负)。TODO:接真 LLM 软排序出 bid(L1)。
            score = self.card.state.battery
            reasons = [f"应征(电量{self.card.state.battery})"]
            if skill_hint:
                reasons.append(f"参考Skill:{skill_hint}")
            decision = ParticipationDecision(device_id=did, decision="accept", bid_score=score,
                                             reason_codes=reasons)
        self.bb.append(BlackboardEvent(
            id=f"bid-{task_id}-{did}", type=EventType.BID, ledger=Ledger.TASK,
            content={"task_id": task_id, "device_id": did, "accept": decision.accept,
                     "decision": decision.decision, "bid_score": decision.bid_score,
                     "reason": "；".join(decision.reason_codes)},
            source=did, idempotency_key=f"bid-{task_id}-{did}"))
        # B5:记模型级细节到 Trace(黑板不承载,Trace 独有的第二数据源)
        if self.trace is not None:
            self.trace.record_model_detail(f"bid-{task_id}-{did}", {
                "situation": situation, "skill_hint": skill_hint,
                "decision": decision.decision})

    def _situation_of(self, task_id: str) -> str:
        """从任务上下文推情境键(MVP:默认映射;真上按 task_type/黑板状态判定)。"""
        # TODO:按 TaskPackage.task_type + 黑板当前态势 推情境,匹配对应 Skill
        return "multi_device_area_coverage"

    def act(self, intent: ActionIntent, context: dict = None):
        """tool_call 环节:loop 自己经 tool_gateway 发起动作(而非 harness 代劳)。

        真 agent loop:decide → act(tool_call)→ observation 回喂。
        安全铁律"拦在每个 tool_call 前":act 是唯一 tool_call 出口,门控夹在此处最省心。
        MVP 直接下发;门控接线点见下方 TODO。
        """
        if self.tool_gateway is None:
            return None
        # TODO:act 前过安全门控(HookPort.pre_tool 契约已钉)——R2 拒 / R1 挂起授权点等人工 / R0 放行。
        #       MVP 不在 loop 内做门控;skeleton 的安全链路走 harness 级 guardrail.check + tool_gateway 急停门控。
        receipt = self.tool_gateway.dispatch(intent)
        # observation 回喂:回执写黑板(动作回执账本)
        self.bb.append(BlackboardEvent(
            id=f"rcpt-{intent.intent_id}", type=EventType.RECEIPT, ledger=Ledger.RECEIPT,
            content={"intent_id": intent.intent_id, "device_id": intent.device_id,
                     "success": receipt.success}, source=intent.device_id))
        return receipt
