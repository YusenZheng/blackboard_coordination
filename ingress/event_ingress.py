# STATUS: NOT-STAGED —— 反向事件流本迭代不实现 / 契约见 interfaces.EventIngressPort / 待实现
"""ingress.event_ingress —— 事件接入(设备/环境事件 → 标准化 → 黑板)。

【A4】实现 contracts.interfaces.EventIngressPort(已上抬为与三出向对称的一等 Protocol)。
【C8】与 task_gen 区分两入口:task_gen 是人的自然语言→任务包;event_ingress 是
设备上报/环境事件(摄像头告警/机器狗发现线索)→ 标准化 → 写黑板。覆盖三源:
    设备报障 / Tool 回执线索 / 环境节点(CAMERA)告警。

TODO:接补充说明 §2.1 事件接入层;api 已可加 POST /events 对接。
"""
from __future__ import annotations


class EventIngress:
    """实现 EventIngressPort。NOT-STAGED:骨架只留形状。"""
    def ingest(self, raw: dict) -> list:
        raise NotImplementedError("NOT-STAGED: 反向事件流待实现(设备/环境事件→黑板),见 EventIngressPort")
