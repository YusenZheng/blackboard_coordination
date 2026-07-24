# STATUS: NOT-STAGED —— 本迭代不实现 / demo 不依赖仿真时可用 sim 顶替 / 待实现
"""access.adapters.drone_adapter —— 无人机真机适配器(PX4 + MAVLink)。

实现提示:pymavlink 直连(不套 MAVROS,少一层封装少一层延迟故障点);
契约动词 move_to/hover/return_home → MAVLink 航点/模式指令;坐标 WGS84 ↔ world 经 frames。
真机没打通时,用 sim.sim_adapter 以 drone 角色顶替(接口一致)。
"""
from .base import BaseAdapter
from ...contracts.task import ActionIntent, ActionReceipt


class DroneAdapter(BaseAdapter):
    def execute(self, intent: ActionIntent) -> ActionReceipt:
        raise NotImplementedError("NOT-STAGED: 无人机真机适配器待实现(PX4/MAVLink)")

    def read_telemetry(self) -> dict:
        raise NotImplementedError("NOT-STAGED")
