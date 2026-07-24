# STATUS: NOT-STAGED —— 本迭代不实现 / 待实现
"""access.adapters.dog_adapter —— 机器狗真机适配器(厂商 SDK / ROS2)。

实现提示:宇树 Go2 高层 API,局部坐标目标点、自带避障;图像/点云/状态经 WebRTC/DDS。
物理约束 min_gap_cm=40(仅进 >40cm 缝隙)写进 CapabilityProfile.width_cm=40。
无全局 GPS 修正有累积漂移,长时间跑要定期校正(经 frames)。
真机没打通时用 sim.sim_adapter 以 dog 角色顶替。
"""
from .base import BaseAdapter
from ...contracts.task import ActionIntent, ActionReceipt


class DogAdapter(BaseAdapter):
    def execute(self, intent: ActionIntent) -> ActionReceipt:
        raise NotImplementedError("NOT-STAGED: 机器狗真机适配器待实现(Go2 SDK)")

    def read_telemetry(self) -> dict:
        raise NotImplementedError("NOT-STAGED")
