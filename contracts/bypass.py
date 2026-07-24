"""contracts.bypass —— 三条合法旁路原语。

【A8】铁律是"一切协同走黑板、禁点对点"。但文档硬要求三处旁路,若不在契约层显式立原语,
否则容易被绕过、也分不清"合理旁路"和"偷懒破例"。故在此把三条旁路
立成一等原语,README 另写"三条合法旁路及其理由"章节。

三条旁路(且仅此三条合法):
    1. TelemetryChannel —— 高频状态(每秒几十条位姿电量)旁路四账本折叠,直更 agent card
       state 快照。但【仍抄送 Trace】(B2:Trace 订阅,别把 Trace 切掉)。
       理由:走 append-only + 四账本折叠全套,内存黑板会被高频流冲爆。
    2. EmergencyStop —— 急停旁路所有软件层(Loop/网关/guardrail)直达设备驱动。
       理由:软件可崩,急停不能崩(OM1 固件级急停 + 安全回路时序隔离)。事件仍留痕。
    3. 授权点 suspend/resume —— 不可逆动作(R1)挂起 Loop 等人工确认。
       走"人类确认账本"(Ledger.HUMAN)寻址,不走派生视图轮询。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


# ── 旁路 1:高频遥测通道 ──────────────────────────────────────────────

@dataclass
class TelemetrySample:
    """一条遥测样本(高频、旁路 LLM、不进 Agent context)。"""
    device_id: str
    battery: float
    position: dict                    # 轻量 dict,不折叠进四账本
    current_action: str = "idle"
    ts: float = 0.0


class TelemetryChannel(Protocol):
    """遥测旁路通道:直更 agent card state + 抄送 Trace,不走四账本折叠。"""
    def push(self, sample: TelemetrySample) -> None:
        """设备定期推送。实现:更新 registry 里的 AgentCard.state + 抄送 Trace。"""
        ...


# ── 旁路 2:急停信号 ──────────────────────────────────────────────────

@dataclass
class EmergencyStop:
    """急停信号(旁路所有软件层直达驱动)。MVP 即使 print 也要有此原语。"""
    scope: str = "all"                # all / <device_id> / <area>
    reason: str = ""
    ts: float = 0.0


class EmergencyStopBus(Protocol):
    """急停总线:signal() 必须绕过 Loop/网关/guardrail 直达设备;事件另留痕。"""
    def signal(self, estop: EmergencyStop) -> None: ...


# ── 旁路 3:授权点挂起/恢复 ───────────────────────────────────────────

@dataclass
class AuthDecision:
    """授权点的人工决定。"""
    intent_id: str
    approved: bool
    approver: str = ""
    reason: str = ""
    ts: float = 0.0


class AuthorizationGate(Protocol):
    """授权点:不可逆动作 R1 挂起 Loop,等人工经"人类确认账本"确认后恢复。"""
    def suspend(self, intent_id: str, context: dict) -> None:
        """挂起该动作,向人类确认账本写 AUTH_POINT 事件,推指挥官。"""
        ...

    def resume(self, intent_id: str, decision: AuthDecision) -> None:
        """人工决定回来,写 AUTH_DECISION 事件,放行或拒绝,记入 Trace。"""
        ...
