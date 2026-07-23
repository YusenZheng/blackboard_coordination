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

import copy

from ..contracts.blackboard_event import BlackboardEvent, EventType, Ledger
from ..contracts.task import ActionIntent
from ..contracts.types import ParticipationDecision
from .action_executor import DeterministicActionPolicy
from .context_assembler import Budget, ContextAssembler
from .models import (
    AgentPublicSnapshot,
    AssignmentSnapshot,
    CoordinationEventType,
    Effect,
    EffectKind,
    IntentSession,
    IntentState,
    LoopInput,
    RoleSlot,
    StepResult,
    TaskSessionState,
    event_content,
    event_type_value,
    make_blackboard_event,
    to_json_value,
)
from .policy import EligibilityBidEngine


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


class PureAgentLoop:
    """v2 AgentLoop: complete input in, deterministic state/effects out, no I/O."""

    def __init__(
        self,
        device_id: str,
        *,
        bid_engine: EligibilityBidEngine | None = None,
        action_policy: DeterministicActionPolicy | None = None,
    ) -> None:
        self.device_id = device_id
        self.bid_engine = bid_engine or EligibilityBidEngine()
        self.action_policy = action_policy or DeterministicActionPolicy()

    def step(self, loop_input: LoopInput) -> StepResult:
        session = copy.deepcopy(loop_input.session)
        event_type = event_type_value(loop_input.event)
        handlers = {
            CoordinationEventType.BID_ROUND_OPENED.value: self._on_bid_round_opened,
            CoordinationEventType.TASK_ASSIGNED.value: self._on_task_assigned,
            CoordinationEventType.ACTION_INTENT.value: self._on_action_intent,
            CoordinationEventType.RECEIPT.value: self._on_receipt,
            CoordinationEventType.SAFETY_INTERCEPT.value: self._on_safety_intercept,
            CoordinationEventType.ASSIGNMENT_COMPLETED.value: self._on_assignment_completed,
            CoordinationEventType.TASK_REPLAN.value: self._on_task_replan,
            CoordinationEventType.TASK_DONE.value: self._on_task_done,
            CoordinationEventType.TASK_FAILED.value: self._on_task_failed,
            CoordinationEventType.ESTOP.value: self._on_estop,
        }
        handler = handlers.get(event_type)
        if handler is None:
            return StepResult([], session, True)
        return handler(loop_input, session)

    def _on_bid_round_opened(self, loop_input: LoopInput, session) -> StepResult:
        content = event_content(loop_input.event)
        if loop_input.task_view is None:
            return StepResult([], session, False)
        task_view = loop_input.task_view
        if task_view.get("terminal_event_id") is not None:
            return StepResult([], session, True)
        if int(content.get("task_revision", 0)) != int(task_view.get("task_revision", 0)):
            return StepResult([], session, True)
        if loop_input.now > float(content.get("deadline", 0.0)):
            return StepResult([], session, True)

        task = dict(task_view.get("task", {}))
        slots_by_id = {
            slot.slot_id: slot
            for slot in [RoleSlot.from_dict(value) for value in task.get("role_slots", [])]
        }
        requested_ids = [str(value) for value in content.get("slots", [])]
        if not requested_ids or any(slot_id not in slots_by_id for slot_id in requested_ids):
            return StepResult([], session, True)
        role_slots = [slots_by_id[slot_id] for slot_id in requested_ids]
        payload = self.bid_engine.make_bid(
            task=task,
            role_slots=role_slots,
            snapshot=loop_input.agent_snapshot,
            execution_availability=loop_input.execution_availability,
            task_revision=int(content["task_revision"]),
            coordination_epoch=int(content["coordination_epoch"]),
            bid_round=int(content.get("bid_round", 1)),
            deadline=float(content["deadline"]),
            now=loop_input.now,
            proposal=loop_input.local_proposal,
            skill_references=loop_input.skill_references,
        )
        event = make_blackboard_event(
            CoordinationEventType.BID,
            Ledger.TASK,
            payload,
            self.device_id,
            (
                f"bid:{payload.task_id}:{payload.task_revision}:"
                f"{payload.coordination_epoch}:{payload.bid_round}:{self.device_id}"
            ),
        )
        session.task_revision = payload.task_revision
        session.coordination_epoch = payload.coordination_epoch
        session.state = TaskSessionState.STANDBY
        session.session_version += 1
        return StepResult(
            [Effect(EffectKind.APPEND_BLACKBOARD_EVENT, event)], session, True
        )

    def _on_task_assigned(self, loop_input: LoopInput, session) -> StepResult:
        content = event_content(loop_input.event)
        task_view = loop_input.task_view
        if not task_view:
            return StepResult([], session, False)
        if task_view.get("terminal_event_id") is not None:
            return StepResult([], session, True)
        if task_view.get("replan_pending"):
            return StepResult([], session, False)
        revision = int(content.get("task_revision", 0))
        epoch = int(content.get("coordination_epoch", 0))
        if revision != int(task_view.get("task_revision", 0)):
            return StepResult([], session, True)
        if epoch != int(task_view.get("coordination_epoch", 0)):
            return StepResult([], session, True)
        if session.coordination_epoch > epoch:
            return StepResult([], session, True)
        current_plan = task_view.get("current_plan")
        if not isinstance(current_plan, dict):
            return StepResult([], session, True)
        if str(content.get("plan_id", "")) != str(current_plan.get("plan_id", "")):
            return StepResult([], session, True)
        assignments = [
            AssignmentSnapshot.from_dict(value) for value in content.get("assignments", [])
        ]
        current_assignments = {
            str(value.get("assignment_id", "")): value
            for value in current_plan.get("assignments", [])
            if isinstance(value, dict)
        }
        for value in assignments:
            current = current_assignments.get(value.assignment_id)
            if current is None:
                return StepResult([], session, True)
            if (
                str(current.get("device_id", "")) != value.device_id
                or int(current.get("assignment_epoch", 0)) != value.assignment_epoch
            ):
                return StepResult([], session, True)
        assignment = next(
            (value for value in assignments if value.device_id == self.device_id), None
        )
        session.task_revision = revision
        session.coordination_epoch = epoch
        session.session_version += 1
        if assignment is None:
            session.state = TaskSessionState.STANDBY
            session.assignment = None
            session.current_intent = None
            return StepResult([], session, True)

        submit = self.action_policy.build_submit_payload(
            task_id=str(content["task_id"]),
            task_revision=int(content["task_revision"]),
            coordination_epoch=int(content["coordination_epoch"]),
            plan_id=str(content["plan_id"]),
            assignment=assignment,
        )
        if session.current_intent and session.current_intent.intent_id == submit.intent.intent_id:
            if session.current_intent.state in (
                IntentState.DISPATCHING,
                IntentState.DISPATCHED,
                IntentState.SUCCEEDED,
                IntentState.FAILED,
                IntentState.SAFETY_BLOCKED,
                IntentState.DISPATCH_UNKNOWN,
            ):
                # 重放 TASK_ASSIGNED 只恢复状态，绝不产生第二次物理下发。
                session.assignment = assignment
                session.state = TaskSessionState.ACTIVE
                return StepResult([], session, True)
        session.state = TaskSessionState.ACTIVE
        session.assignment = assignment
        session.current_intent = IntentSession(
            intent_id=submit.intent.intent_id,
            state=IntentState.PROPOSED,
            intent_fingerprint=submit.intent_fingerprint,
        )
        return StepResult(
            [Effect(EffectKind.SUBMIT_ACTION_INTENT, submit)], session, True
        )

    def _on_action_intent(self, loop_input: LoopInput, session) -> StepResult:
        content = event_content(loop_input.event)
        current = session.current_intent
        if current and content.get("intent_id") == current.intent_id:
            # 回读只核对，不再次生成 submit Effect。
            if current.state == IntentState.PROPOSED:
                current.state = IntentState.DISPATCHED
                session.session_version += 1
        return StepResult([], session, True)

    def _on_receipt(self, loop_input: LoopInput, session) -> StepResult:
        content = event_content(loop_input.event)
        current = session.current_intent
        if current is None or content.get("intent_id") != current.intent_id:
            return StepResult([], session, True)
        receipt_id = str(content.get("receipt_id", ""))
        success = (
            content.get("success") is True
            and content.get("outcome_certainty") == "confirmed"
            and content.get("post_check_allowed") is True
        )
        current.state = IntentState.SUCCEEDED if success else (
            IntentState.DISPATCH_UNKNOWN
            if content.get("outcome_certainty") == "unknown"
            else IntentState.FAILED
        )
        current.receipt_id = receipt_id or None
        session.last_receipt_id = receipt_id or None
        if not success:
            session.state = TaskSessionState.FAILED
        session.session_version += 1
        return StepResult([], session, True)

    def _on_safety_intercept(self, loop_input: LoopInput, session) -> StepResult:
        content = event_content(loop_input.event)
        current = session.current_intent
        if current and content.get("intent_id") == current.intent_id:
            current.state = IntentState.SAFETY_BLOCKED
            session.state = TaskSessionState.FAILED
            session.session_version += 1
        return StepResult([], session, True)

    def _on_assignment_completed(self, loop_input: LoopInput, session) -> StepResult:
        content = event_content(loop_input.event)
        if session.assignment and content.get("assignment_id") == session.assignment.assignment_id:
            session.state = TaskSessionState.DONE
            session.session_version += 1
        return StepResult([], session, True)

    def _on_task_replan(self, loop_input: LoopInput, session) -> StepResult:
        content = event_content(loop_input.event)
        if int(content.get("from_epoch", -1)) != session.coordination_epoch:
            return StepResult([], session, True)
        session.coordination_epoch = int(content["to_epoch"])
        session.state = TaskSessionState.OBSERVED
        session.assignment = None
        session.current_intent = None
        session.last_receipt_id = None
        session.session_version += 1
        return StepResult([], session, True)

    def _on_task_done(self, loop_input: LoopInput, session) -> StepResult:
        session.state = TaskSessionState.DONE
        session.session_version += 1
        return StepResult(
            [Effect(EffectKind.CLEANUP_TASK_SESSION, session.task_id)], session, True
        )

    def _on_task_failed(self, loop_input: LoopInput, session) -> StepResult:
        session.state = TaskSessionState.FAILED
        session.session_version += 1
        return StepResult(
            [Effect(EffectKind.CLEANUP_TASK_SESSION, session.task_id)], session, True
        )

    def _on_estop(self, loop_input: LoopInput, session) -> StepResult:
        content = event_content(loop_input.event)
        scope_type = content.get("scope_type")
        scope_id = content.get("scope_id")
        affected = scope_type == "all" or (
            scope_type == "device" and scope_id == self.device_id
        )
        if content.get("operation") == "stop" and affected:
            session.state = TaskSessionState.FAILED
            session.session_version += 1
        return StepResult([], session, True)
