# STATUS: STAGED(A类,样板)—— G01 定点导航样例。★ 补其余 Tool 照这个文件的样子写
"""access.tools.g01_navigate —— G01 定点导航(样例 Tool)。

IO 字段严格照《寻找小白_ToolSkill_IO设计_v1.2》G01 定点导航:
    用途:自主导航至指定坐标点(避障/局部路径属内部实现)。复用事件:E05/E12/E13/E15/E17。
mock 实现:print 一下、返回到达状态。真实现由适配器接(见 adapters/)。

【这是"一个 Tool 长什么样"的标准样板——照这个补 G02-Y08。】
"""
from __future__ import annotations

from .base import ToolSpec


def _run_g01(params: dict) -> dict:
    """mock 执行:接受 device/destination/movement_mode,返回到达状态。"""
    dest = params.get("destination")
    print(f"    [G01定点导航] → {dest}(mock 到达)")
    return {
        "status": "到达",
        "current_position": dest,
        "route_summary": "mock 直达,无障碍",
        "constraint_notes": [],
    }


G01_NAVIGATE = ToolSpec(
    tool_id="G01",
    name="定点导航",
    tool_class="G",
    input_schema={
        "device": "DeviceRef",
        "destination": "Position",
        "route_hint": "str?",
        "movement_mode": "str  # 飞行/平地行驶/四足行走/匍匐",
        "constraints": "list<str>?",
    },
    output_schema={
        "status": "str  # 到达/行进中/受阻",
        "current_position": "Position",
        "route_summary": "str",
        "constraint_notes": "list<str>?",
    },
    reuse_events=["E05", "E12", "E13", "E15", "E17"],
    run=_run_g01,
)
