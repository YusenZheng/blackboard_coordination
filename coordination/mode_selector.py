# STATUS: STAGED(A类)—— MVP:自主应征 + 集中式两挡真分流;workflow/单机建分支占位
"""coordination.mode_selector —— 协同模式选择器(维度一)。实现 ModeSelectorPort。

按任务标签自动路由挡位。本轮:自主应征 + 集中式(master-worker)两挡真分流
——证明"分配挡位可插拔"。挡位是"用哪种组织方式"的架构选择(框架预置可插拔),
不是让模型每次临时发明。runtime 按 route() 返回值决定走 loop 竞价 还是 master 点名。
"""
from __future__ import annotations

from enum import Enum

from ..contracts.task import TaskPackage


class Mode(str, Enum):
    AUTONOMOUS = "autonomous"       # 自主应征:发黑板→各loop竞价bid→招投标(去中心,涌现)
    MASTER_WORKER = "master_worker" # 集中式:master 点名指派(中心,可控)
    WORKFLOW = "workflow"           # 预定义 SOP(TODO)
    SINGLE = "single"               # 单机直控(TODO)


class ModeSelector:
    def route(self, task: TaskPackage) -> str:
        """返回挡位名。任务包 extra.mode(北向 task_gen 分流已定调)优先;否则 MVP 默认自主应征。"""
        mode = task.extra.get("mode")
        if mode:
            return mode
        # MVP:默认自主应征。按任务三标签([目标清晰度][环境开放度][风险])真路由到
        # master_worker/workflow/single 的判定 —— TODO,别用返回同值的假分支冒充。
        return Mode.AUTONOMOUS.value
