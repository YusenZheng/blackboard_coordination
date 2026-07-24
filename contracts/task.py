"""contracts.task —— 任务包 / 动作意图 / 动作回执。

一次任务在系统里的三个关键载体:
    TaskPackage  —— 北向 ingress 把自然语言/事件融合成的结构化任务(发布到黑板)
    ActionIntent —— 协同层 Agent Loop 产出、要下发的动作意图(过安全平面 → Tool 网关)
    ActionReceipt—— 设备执行后的回执(回写黑板动作回执账本)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .types import Position, TaskRequirement
from .verbs import ActionVerb


@dataclass
class TaskPackage:
    """结构化空间行动任务(对齐补充说明:带位置/时间/物品/权限/物理约束/能力需求/反馈)。"""
    task_id: str
    task_type: str                       # 如 "search_target"
    goal: str                            # 任务目标(自然语言)
    success_condition: str = ""          # 成功条件
    safety_constraints: list = field(default_factory=list)   # 安全红线/约束
    requirement: Optional[TaskRequirement] = None            # 能力需求(bid 硬过滤用)
    target_profile: dict = field(default_factory=dict)       # 目标画像(照片/特征/习性…)
    area: Optional[Position] = None                          # 任务区域
    priority: str = "normal"             # low / normal / high / urgent
    # C6:初始信任等级由路由打标(ingress.route)决定,autonomy policy 读它
    initial_autonomy_level: str = "A0"
    extra: dict = field(default_factory=dict)


@dataclass
class ActionIntent:
    """待执行的动作意图(协同层产出 → 安全校验 → Tool 网关翻译成设备指令)。"""
    intent_id: str
    device_id: str                       # 由哪个设备执行
    verb: ActionVerb                     # 动作动词
    params: dict = field(default_factory=dict)   # 如 {"target": Position}
    reversible: bool = True              # 可逆性(safety.reversibility 判 R0/R1/R2)
    task_id: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class ActionReceipt:
    """动作执行回执(回写黑板动作回执账本 + Trace 派生落档)。"""
    intent_id: str
    device_id: str
    success: bool
    result: dict = field(default_factory=dict)
    failure_reason: Optional[str] = None  # A11:失败原因显式字段
    # B4:失败恢复分类 —— 让执行失败后有"半环"可走(retry/改派/升级人工/不可恢复)。
    # 骨架只定字段;恢复策略汇入 coordination.on_replan,留算法组 L1 实现。
    recovery_class: Optional[str] = None  # retryable / need_reassign / need_human / unrecoverable
    partial_progress: Optional[dict] = None  # 部分完成进度(可选)
    duration_s: float = 0.0
    extra: dict = field(default_factory=dict)
