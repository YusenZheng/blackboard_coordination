# STATUS: STAGED(A类)—— MVP 可跑:mock 适配器,print 动作、返回成功回执
"""access.adapters.mock_adapter —— MVP mock 适配器(三类设备通用假的)。

skeleton 用它跑通链路:执行动作 = print + 返回成功回执。无物理约束/坐标/时延。
真机版见 drone_adapter/vehicle_adapter/dog_adapter(NOT-STAGED)。
"""
from __future__ import annotations

from ...contracts.task import ActionIntent, ActionReceipt
from .base import BaseAdapter


class MockAdapter(BaseAdapter):
    def execute(self, intent: ActionIntent) -> ActionReceipt:
        cmd = self.translate(intent.verb, intent.params)
        print(f"  [执行] 设备 {self.device_id} 执行 {intent.verb.value} "
              f"参数={intent.params}")
        return ActionReceipt(intent_id=intent.intent_id, device_id=self.device_id,
                             success=True, result={"cmd": cmd}, duration_s=0.1)

    def read_telemetry(self) -> dict:
        return {"device_id": self.device_id, "battery": 0.9, "action": "idle"}
