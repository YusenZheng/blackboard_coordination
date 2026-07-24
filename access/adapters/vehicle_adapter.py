# STATUS: NOT-STAGED —— 本迭代不实现 / 待实现
"""access.adapters.vehicle_adapter —— 无人车真机适配器(rclpy 封 ROS2 / 自研 SDK)。

实现提示:契约动词 → ROS2 Nav2 action;室外无全局定位靠 RTK 或定位融合(经 frames)。
真机没打通时用 sim.sim_adapter 以 vehicle 角色顶替。
"""
from .base import BaseAdapter
from ...contracts.task import ActionIntent, ActionReceipt


class VehicleAdapter(BaseAdapter):
    def execute(self, intent: ActionIntent) -> ActionReceipt:
        raise NotImplementedError("NOT-STAGED: 无人车真机适配器待实现(ROS2/Nav2)")

    def read_telemetry(self) -> dict:
        raise NotImplementedError("NOT-STAGED")
