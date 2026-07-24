# STATUS: STAGED(A类)—— MVP:装配六层 + 黑板事件驱动主循环 + 重新协同入口
"""runtime.harness —— Harness 主循环(黑板事件驱动)。

【B4】主循环写成【黑板事件驱动】而非一次性单向:黑板新事件 → 各 loop.on_event 重协同。
MVP 已驱动:TASK_POSTED→bid、CLUE→重协同(trigger_replan)。

【C4 边界】本迭代事件驱动仅 "task_gen→黑板 TASK_POSTED→loop.bid" 正向 + "CLUE→重协同" 通;
    设备/环境主动上报的反向入口(ingress.event_ingress)整体 NOT-STAGED,后续实现。

装配六层 + 两独立顶层。这是把 contracts 定的接口用真实现接起来的地方。
"""
from __future__ import annotations

from ..access.registry import Registry
from ..access.tool_gateway import ToolGateway
from ..assets.ledger import Ledger
from ..assets.skill import load_builtin_skills
from ..assets.trace import Trace
from ..blackboard.board import Blackboard
from ..coordination.agent_loop import AgentLoop
from ..coordination.conflict import ConflictService
from ..coordination.context_assembler import ContextAssembler
from ..coordination.mode_selector import ModeSelector
from ..ingress.task_gen import TaskGen
from ..safety.guardrail import Guardrail
from ..safety.reversibility import MockAuthorizationGate


class Harness:
    """群体 Agent Harness 运行时:装配所有层。"""

    def __init__(self, adapters: dict = None, *, intent_interpreter=None):
        # 地基
        self.blackboard = Blackboard()
        self.trace = Trace()
        # 安全(先建急停总线,ToolGateway 要用它做下发前门控)
        self.guardrail = Guardrail()
        self.auth_gate = MockAuthorizationGate(blackboard=self.blackboard, trace=self.trace)
        from ..safety.estop import MockEmergencyStopBus
        self.estop = MockEmergencyStopBus(blackboard=self.blackboard)                # B3
        # 接入(Tool 网关注入急停总线 → 下发前查急停 B1)
        self.registry = Registry()
        # 高频状态仍留在 Registry/Telemetry 旁路；Blackboard 只在协同决策点
        # 将其投影成 agent_public 快照，不把每条遥测灌入事件流。
        self.blackboard.set_agent_snapshot_provider(self.registry)
        self.tool_gateway = ToolGateway(
            adapters=adapters or {},
            estop_bus=self.estop,
            device_registry=self.registry,
        )
        # 协同(含 Skill Graph:Agent Loop 检索经验参考)
        self.assembler = ContextAssembler()
        self.mode_selector = ModeSelector()
        self.conflict = ConflictService(self.blackboard)
        # 资产
        self.ledger = Ledger(trace=self.trace)
        self.skill_graph = load_builtin_skills()
        # 旁路(A8 三条合法旁路:遥测 + 急停[上面已建] + 授权点[auth_gate])
        from ..access.telemetry import TelemetryChannel
        self.telemetry = TelemetryChannel(registry=self.registry, trace=self.trace)  # B4
        # 北向
        self.task_gen = TaskGen(intent_interpreter=intent_interpreter)
        self.intent_interpreter = intent_interpreter
        # 每设备一个云端虚拟 Agent 的 loop
        self._loops: dict = {}

        # B2:Trace 订阅黑板事件流(派生落档,不双写)
        self.blackboard.subscribe(self.trace.on_event)

    def spawn_agent(self, card) -> AgentLoop:
        """为一台注册设备生成云端虚拟 Agent 的常驻 loop(注入 skill_graph + tool_gateway + trace)。"""
        self.registry.register(card)
        loop = AgentLoop(card, self.blackboard, self.assembler,
                         skill_graph=self.skill_graph, tool_gateway=self.tool_gateway,
                         trace=self.trace, guardrail=self.guardrail, auth_gate=self.auth_gate)
        self._loops[card.identity.device_id] = loop
        # 黑板事件驱动:任务发布事件 → 各 loop on_event 决定应征
        self.blackboard.subscribe(loop.on_event)
        return loop

    def loops(self) -> dict:
        return self._loops

    def make_master(self):
        """按需构造集中式编排器(master-worker 挡)。与自主应征挡并存、可插拔。"""
        from ..coordination.master import MasterOrchestrator
        return MasterOrchestrator(self.registry, self.blackboard)

    def trigger_replan(self, task_id: str, clue_id: str) -> int:
        """B6:新线索驱动重协同 —— 让各 loop 就地重新评估(黑板状态变→计划跟着变)。

        MVP:通知每个 loop 的重协同入口(真上是线索融合 Y03 + 重规划策略,留 TODO)。
        返回参与重评估的设备数。
        """
        n = 0
        for loop in self._loops.values():
            loop.on_replan(task_id, clue_id)
            n += 1
        return n
