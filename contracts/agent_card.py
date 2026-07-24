"""contracts.agent_card —— Agent Card 四件套。

【核心定性】设备接入后 = 一个 Agent(不是纯 Tool Provider)。上层(协同/黑板)把它当
自主成员协作,不认设备型号。动作契约 + 原子 Tool + 社交动词是这个 Agent"能力"那一栏。

四件套(对齐 v5 支柱一 + 主心骨接入层):
    身份 Identity —— 我是谁
    状态 State    —— 我现在怎么样(Telemetry 高频更新)
    履历 Resume   —— 我过去表现怎么样(信任分级/派单质量的依据,"越用越懂"的载体)
    能力 Capability —— 我会什么(动作契约动词 + 原子 Tool + 社交动词)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .types import CapabilityProfile, DeviceRef, DeviceState
from .verbs import ActionVerb, SocialVerb


@dataclass
class Resume:
    """履历 —— 会成长的简历(C14:靠动作回执账本累积;autonomy 读它给等级建议)。

    MVP:success_rate = 成功/总,纯统计。复杂履历实现留 TODO。
    """
    tasks_done: int = 0
    tasks_total: int = 0
    handoff_success: int = 0
    anomaly_count: int = 0
    human_takeover_count: int = 0
    familiar_areas: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return self.tasks_done / self.tasks_total if self.tasks_total else 0.0


@dataclass
class CapabilitySlot:
    """能力那一栏 = 三件套。契约动词固定统一,原子 Tool 按设备实际暴露。"""
    action_verbs: list = field(default_factory=lambda: [])   # list[ActionVerb] 设备支持的动作动词
    social_verbs: list = field(default_factory=lambda: [      # list[SocialVerb] 默认全支持
        SocialVerb.BID, SocialVerb.CLAIM, SocialVerb.YIELD,
        SocialVerb.HANDOFF, SocialVerb.ASK_HELP,
    ])
    atomic_tools: list = field(default_factory=list)          # 原子 Tool 名(G01定点导航/G02区域扫描…)
    profile: CapabilityProfile = field(default_factory=CapabilityProfile)  # 物理能力/约束
    extra: dict = field(default_factory=dict)


@dataclass
class AgentCard:
    """一个接入设备的完整成员画像。registry 负责注册生成,能力变化时刷新。"""
    identity: DeviceRef
    state: DeviceState = field(default_factory=DeviceState)
    resume: Resume = field(default_factory=Resume)
    capability: CapabilitySlot = field(default_factory=CapabilitySlot)
    # 初始信任等级由 ingress 路由打标给出(C6),autonomy policy 读它
    initial_autonomy_level: str = "A0"
    extra: dict = field(default_factory=dict)
