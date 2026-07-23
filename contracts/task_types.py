"""contracts.task_types —— 无人设备任务类型候选目录。

``task_type`` 是跨层传递的稳定语义标识：北向入口负责识别，协同、Skill、
安全和资产层按需消费。本目录只描述“任务是什么”，不绑定具体设备、Tool、
协同模式或自治等级。

``TaskPackage.task_type`` 继续保持 ``str``，目录项用于约束可选值、生成模型提示
和展示中文名称。新增类型时应保持已有 ``task_type`` 标识不变。
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class TaskTypeDefinition:
    """一个可供意图理解选择的任务类型。"""

    task_type: str
    name: str
    definition: str


SEARCH_TARGET = "search_target"
PATROL_AREA = "patrol_area"
INSPECT_ASSET = "inspect_asset"
SURVEY_AREA = "survey_area"
MAP_AREA = "map_area"
MONITOR_AREA = "monitor_area"
TRACK_TARGET = "track_target"
VERIFY_ALARM = "verify_alarm"
TRANSPORT_PAYLOAD = "transport_payload"
COLLECT_SAMPLE = "collect_sample"
MEASURE_ENVIRONMENT = "measure_environment"
RELAY_COMMUNICATION = "relay_communication"
GUARD_PERIMETER = "guard_perimeter"
ASSESS_HAZARD = "assess_hazard"
ASSIST_RESCUE = "assist_rescue"
INVENTORY_ASSETS = "inventory_assets"


TASK_TYPE_CATALOG: tuple[TaskTypeDefinition, ...] = (
    TaskTypeDefinition(
        task_type=SEARCH_TARGET,
        name="寻找指定目标",
        definition="在指定或推定区域内搜索并确认某个人、动物、车辆、物品或异常目标。",
    ),
    TaskTypeDefinition(
        task_type=PATROL_AREA,
        name="区域巡逻",
        definition="按照指定路线或覆盖策略巡查一个区域，并持续上报发现的异常和状态变化。",
    ),
    TaskTypeDefinition(
        task_type=INSPECT_ASSET,
        name="设施巡检",
        definition="对指定设施、设备或结构采集状态信息，识别缺陷、损坏或运行异常。",
    ),
    TaskTypeDefinition(
        task_type=SURVEY_AREA,
        name="区域勘测",
        definition="对指定区域采集地形、空间、环境或可通行性数据，形成一次性勘测结果。",
    ),
    TaskTypeDefinition(
        task_type=MAP_AREA,
        name="环境建图",
        definition="建立或更新指定区域的空间地图、障碍物信息和可通行区域。",
    ),
    TaskTypeDefinition(
        task_type=MONITOR_AREA,
        name="区域监测",
        definition="在一段时间内持续或周期性观察指定区域和点位，并报告变化或异常。",
    ),
    TaskTypeDefinition(
        task_type=TRACK_TARGET,
        name="目标跟踪",
        definition="持续观测一个已知目标，更新其位置、运动方向和当前状态。",
    ),
    TaskTypeDefinition(
        task_type=VERIFY_ALARM,
        name="告警复核",
        definition="前往告警位置采集现场证据，确认告警是否真实并报告现场情况。",
    ),
    TaskTypeDefinition(
        task_type=TRANSPORT_PAYLOAD,
        name="载荷运输",
        definition="将物资、样本或设备从指定起点运输到指定终点并确认交付结果。",
    ),
    TaskTypeDefinition(
        task_type=COLLECT_SAMPLE,
        name="样本采集",
        definition="在指定位置获取物质、生物或环境样本，并保留采集位置和时间等信息。",
    ),
    TaskTypeDefinition(
        task_type=MEASURE_ENVIRONMENT,
        name="环境测量",
        definition="在指定位置或区域测量温度、湿度、气体、辐射、噪声等环境指标。",
    ),
    TaskTypeDefinition(
        task_type=RELAY_COMMUNICATION,
        name="通信中继",
        definition="移动或驻留在指定位置，为其他设备或人员建立、扩展或恢复通信链路。",
    ),
    TaskTypeDefinition(
        task_type=GUARD_PERIMETER,
        name="周界警戒",
        definition="监视指定边界或封控区域，发现并报告未经授权的进入、离开或越界行为。",
    ),
    TaskTypeDefinition(
        task_type=ASSESS_HAZARD,
        name="风险评估",
        definition="进入或接近潜在危险区域采集证据，识别危险源并形成风险情况报告。",
    ),
    TaskTypeDefinition(
        task_type=ASSIST_RESCUE,
        name="救援协助",
        definition="在救援任务中提供现场侦察、人员引导、物资投送或通信支持。",
    ),
    TaskTypeDefinition(
        task_type=INVENTORY_ASSETS,
        name="资产盘点",
        definition="识别并统计指定区域内的设备、物资或其他资产，记录其位置和状态。",
    ),
)


_task_type_index = {item.task_type: item for item in TASK_TYPE_CATALOG}
if len(_task_type_index) != len(TASK_TYPE_CATALOG):
    raise RuntimeError("task_type definitions must use unique identifiers")

TASK_TYPE_BY_ID: Mapping[str, TaskTypeDefinition] = MappingProxyType(
    _task_type_index
)
SUPPORTED_TASK_TYPES = frozenset(TASK_TYPE_BY_ID)


def get_task_type_definition(task_type: str) -> TaskTypeDefinition | None:
    """按稳定标识查询任务类型；未知类型返回 ``None``。"""

    return TASK_TYPE_BY_ID.get(task_type)
