"""北向入口可识别的稳定任务类型目录。"""
from __future__ import annotations
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

@dataclass(frozen=True)
class TaskTypeDefinition:
    task_type: str
    name: str
    definition: str

SEARCH_TARGET = "search_target"
_TASKS = {
    SEARCH_TARGET: ("寻找指定目标", "在指定或推定区域内搜索并确认目标。"),
    "patrol_area": ("区域巡逻", "按照路线或覆盖策略巡查区域并报告异常。"),
    "inspect_asset": ("设施巡检", "采集设施状态并识别缺陷或异常。"),
    "survey_area": ("区域勘测", "采集区域地形、空间或环境数据。"),
    "map_area": ("环境建图", "建立或更新区域地图和可通行信息。"),
    "monitor_area": ("区域监测", "持续观察区域并报告变化或异常。"),
    "track_target": ("目标跟踪", "持续观测并更新已知目标的位置和状态。"),
    "verify_alarm": ("告警复核", "采集现场证据确认告警是否真实。"),
    "transport_payload": ("载荷运输", "将物资或样本运输到指定位置并确认交付。"),
    "collect_sample": ("样本采集", "在指定位置获取并记录样本。"),
    "measure_environment": ("环境测量", "测量指定位置或区域的环境指标。"),
    "relay_communication": ("通信中继", "建立、扩展或恢复通信链路。"),
    "guard_perimeter": ("周界警戒", "监视边界并报告越界行为。"),
    "assess_hazard": ("风险评估", "采集证据并识别潜在危险源。"),
    "assist_rescue": ("救援协助", "提供救援侦察、引导、投送或通信支持。"),
    "inventory_assets": ("资产盘点", "识别、统计区域内资产及其状态。"),
}
TASK_TYPE_CATALOG = tuple(TaskTypeDefinition(k, v[0], v[1]) for k, v in _TASKS.items())
TASK_TYPE_BY_ID: Mapping[str, TaskTypeDefinition] = MappingProxyType({x.task_type: x for x in TASK_TYPE_CATALOG})
SUPPORTED_TASK_TYPES = frozenset(TASK_TYPE_BY_ID)

def get_task_type_definition(task_type: str) -> TaskTypeDefinition | None:
    return TASK_TYPE_BY_ID.get(task_type)
