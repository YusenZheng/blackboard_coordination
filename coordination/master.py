# STATUS: STAGED(A类,样板)—— 集中式(master-worker)挡最小样板。★ 真调度策略后续实现
"""coordination.master —— 集中式编排挡(master-worker)最小样板。

【与自主应征挡对照 —— 证明"分配挡位可插拔"这个架构主张】
    自主应征挡:任务发黑板 → 各设备 loop 自己 bid → 招投标判给(去中心,涌现)。
    集中式挡(本文件):master 读 registry 候选 → 按能力/电量【点名指派】worker(中心,可控)。
两挡对同一个任务走两条不同的路,由 mode_selector 按挡位名分流。

【这是"一个新挡位怎么挂"的样板,此处不含真调度算法】master 的真调度策略
(全局最优/MILP/负载均衡…)留待实现;这里只给 20 行 mock:选第一个够格的设备点名。
"""
from __future__ import annotations

from ..contracts.blackboard_event import BlackboardEvent, EventType, Ledger
from ..contracts.task import TaskPackage


class MasterOrchestrator:
    """集中式编排:master 点名指派,不等竞价。"""

    def __init__(self, registry, blackboard):
        self.registry = registry
        self.bb = blackboard

    def assign(self, task: TaskPackage) -> str:
        """master 读候选 → 点名指派最合适的 worker。返回被指派的 device_id。

        mock 策略:选电量最高的在线候选(真调度策略如全局最优/MILP/负载均衡,后续实现。
        """
        candidates = self.registry.discover(task.requirement)
        if not candidates:
            return None
        # mock:电量最高者(真上换全局最优求解)
        best = max(candidates, key=lambda c: c.state.battery)
        did = best.device.device_id
        # 集中式:直接写"判给"事件,不经 bid 竞价环节
        self.bb.append(BlackboardEvent(
            id=f"assign-{task.task_id}", type=EventType.TASK_ASSIGNED, ledger=Ledger.TASK,
            content={"task_id": task.task_id, "device_id": did, "by": "master(集中式点名)"},
            source="master"))
        print(f"  [集中式] master 点名指派 → {did}(电量{best.state.battery},不经竞价)")
        return did
