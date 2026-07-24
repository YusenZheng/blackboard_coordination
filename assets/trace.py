# STATUS: STAGED(A类)—— MVP 可跑:订阅黑板事件流落内存冷档 + 收模型级细节
"""assets.trace —— Trace 独立档案。实现 contracts.interfaces.TracePort。

【B2】黑板是协同事件唯一写入源;Trace 通过 subscribe 订阅黑板事件流【派生落档】(不双写)。
Trace 另收【模型级细节】(prompt/中间推理/原始数据引用)——黑板不承载,Trace 独有。
独立存储、独立生命周期(冷档、永久、可含大文件),但协同事件单源黑板,不制造两份真相。

TODO:真上落冷存储(对象存储 + 时序库);E-level 阶段聚合视图
(对齐附录B 七阶段:发起/应征组队/搜索/线索收拢/低电补位/找到/沉淀),重建阶段时间线。
"""
from __future__ import annotations

from ..contracts.blackboard_event import BlackboardEvent


class Trace:
    def __init__(self) -> None:
        self._events: list = []          # 派生自黑板的协同事件冷档
        self._model_details: dict = {}   # 模型级细节(Trace 独有,不回黑板)
        self._telemetry: dict = {}       # C10:高频遥测单开一桶(与模型细节分离)

    def on_event(self, event: BlackboardEvent, offset: int) -> None:
        """黑板事件订阅回调 → 落冷档(派生,不双写)。"""
        self._events.append((offset, event))

    def record_model_detail(self, intent_id: str, detail: dict) -> None:
        """记模型级细节(prompt/推理),黑板不承载。"""
        self._model_details.setdefault(intent_id, []).append(detail)

    def record_telemetry(self, device_id: str, sample: dict) -> None:
        """C10:高频物理遥测单开一桶(存储/查询/保留策略与模型推理细节不同,别混灌)。"""
        self._telemetry.setdefault(device_id, []).append(sample)

    # 供 eval/ledger/resume 三处同源取数
    def all_events(self) -> list:
        return list(self._events)
