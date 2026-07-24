"""MVP L1 Skill combining availability analysis with physical navigation."""
from __future__ import annotations

from ..skill import Skill, SkillLevel


SAFE_SEARCH_EXECUTION = Skill(
    signature="safe_search_execution:battery_aware_navigation",
    preconditions=["设备在线健康", "电量裕度满足任务要求", "G01 可绑定"],
    strategy="先核对设备能力与电量裕度，再执行可逆的区域导航；失败时保留明确回执。",
    termination="导航回执确认成功，或出现安全拦截/能力不可用。",
    evidence=["MVP Tool 调用闭环", "RECEIPT 可关联 Trace"],
    version_lineage=["v1.0@MVP"],
    dependencies=["Y06 设备能力查询", "S11 电量裕度分析", "G01 定点导航"],
    level=SkillLevel.L1,
    trigger={"task_type": "search_target", "capability_ids": ["search"]},
    tool_chain=["Y06", "S11", "G01"],
    extra={"title": "电量感知安全搜索", "version": "1.0.0", "status": "active"},
)
