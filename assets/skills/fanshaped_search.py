# STATUS: STAGED(A类,样板)—— 扇形搜索分工 Skill 样例。★ 补其余 Skill 照这个写
"""assets.skills.fanshaped_search —— 扇形搜索分工(Level 1 组合技能样例)。

七元组填法示范。对齐 IO 设计"扇形搜索分工"(复用事件 E04/E05/E15)。
signature = trigger 匹配键;Agent Loop 决策前用它 match,命中作经验参考。

【这是"一个 Skill 长什么样"的标准样板——照这个补其余 4+2 个。】
"""
from __future__ import annotations

from ..skill import Skill, SkillLevel


FANSHAPED_SEARCH = Skill(
    signature="fanshaped_search:multi_device_area_coverage",  # trigger 匹配键
    preconditions=["已完成基础分区(Y02)", "≥2 设备可用"],
    strategy="在 Y02 基础分配上形成扇形推进或三设备收拢;避免与 Y02 重复决定同一件事",
    termination="覆盖率达标 或 出现高置信线索转定位",
    evidence=["E04 首轮分区", "E05 第一轮扇形推进"],
    version_lineage=["v1.0@出厂"],
    dependencies=["Y02 分区分配"],
    level=SkillLevel.L1,
    trigger={"situation": "multi_device_area_coverage", "min_devices": 2},
    tool_chain=["Y02", "G01", "G02", "G03", "G04", "G06"],
)
