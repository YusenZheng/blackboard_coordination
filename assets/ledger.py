# STATUS: STAGED(A类)—— MVP:从 Trace 聚合最简运营账本(与 metrics/resume 同源)
"""assets.ledger —— 运营账本 + 服务目录。

【C11】与黑板"四账本"同名不同物:黑板四账本 = 协同事件账本;这里 = 运营/商业账本
(任务量/设备利用率/服务履约率/区域任务密度/人工接管次数),CFO 看的,是 v5 商业主线承重项。

【三处同源】ledger / eval.metrics / registry.resume 都从 Trace 取数,共享底层采集,
一次埋点三处复用,别各自变成"想用同一批数据却谁都没定契约的孤儿"。

TODO:复盘 Agent 生成成本解释("这单为什么贵 40%");service_catalog 落地。
"""
from __future__ import annotations

from ..contracts.blackboard_event import EventType


class Ledger:
    """运营账本(MVP:从 Trace 事件聚合几个数)。"""
    def __init__(self, trace=None):
        self._trace = trace

    def summary(self) -> dict:
        """聚合运营指标。MVP:任务数/回执数/接管数。"""
        if self._trace is None:
            return {}
        events = [e for _off, e in self._trace.all_events()]
        return {
            "tasks": sum(1 for e in events if e.type == EventType.TASK_POSTED),
            "receipts": sum(1 for e in events if e.type == EventType.RECEIPT),
            "human_takeovers": sum(1 for e in events if e.type == EventType.AUTH_POINT),
        }


# TODO:service_catalog —— 把 Skill 组合成客户可购买的服务
# (园区寻物/活动保障/窄巷接力,含 Skill/设备要求/SLA/成本模型/推荐自治等级)。留 Protocol 占位。
