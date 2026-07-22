# STATUS: STAGED(A类)—— MVP:遥测旁路占位(实现 TelemetryChannel 形状)
"""access.telemetry —— 高频遥测旁路。实现 contracts.bypass.TelemetryChannel。

【A8 旁路1】高频状态(每秒几十条位姿电量)旁路四账本折叠,直更 registry 的 AgentCard.state
快照,【仍抄送 Trace】。不进 Agent context(否则冲爆 token 预算)。遥测给系统看,tool_call 给模型看。

TODO:真上独立快通道(UDP/MQTT);抄送 Trace 走异步批量。
"""
from __future__ import annotations

import time

from ..contracts.bypass import TelemetrySample


class TelemetryChannel:
    def __init__(self, registry=None, trace=None):
        self._registry = registry
        self._trace = trace

    def push(self, sample: TelemetrySample) -> None:
        """设备定期推送 → 更新 AgentCard.state + 抄送 Trace(不走四账本折叠)。"""
        if self._registry is not None:
            card = self._registry.get(sample.device_id)
            if card is not None:
                card.state.battery = sample.battery
                card.state.current_action = sample.current_action
                card.state.extra["state_updated_at"] = float(sample.ts or time.time())
        if self._trace is not None:
            self._trace.record_telemetry(sample.device_id, sample.__dict__)
