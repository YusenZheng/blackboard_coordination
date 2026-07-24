# STATUS: STAGED(A类)—— MVP:恒等坐标变换占位(与 contracts.Position.coordinate_system 收敛一处)
"""access.frames —— 坐标系统一 + 时间同步。

【主心骨点名硬骨头 + A12/C10】空地异构:无人机 3D 空域 GPS/RTK vs 地面 2D 里程计/SLAM,
必须统一世界坐标对齐。与 contracts.types.Position.coordinate_system 收敛在此处理(不是两套活)。

TODO:真变换接 RTK/GPS ↔ 里程计标定;时间同步接 PTP/NTP。室内 room-level 做法不适用室外。
"""
from __future__ import annotations

from ..contracts.types import Position


def to_world(pos: Position) -> Position:
    """把任意坐标系的位置转到统一世界坐标。MVP:恒等占位(直接返回)。

    TODO:按 pos.coordinate_system 查标定参数做真变换(RTK/GPS ↔ 里程计/SLAM)。
    """
    return pos


# sync_time 已移除(骨架无调用;真上接 PTP/NTP 时按 frames TODO 补)
