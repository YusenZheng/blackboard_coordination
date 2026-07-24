"""Representative non-device Tool definitions for the MVP catalog."""
from __future__ import annotations

from .base import ToolSpec


def _battery_analysis(arguments: dict) -> dict:
    battery = float(arguments["battery"])
    threshold = float(arguments.get("threshold", 0.2))
    return {
        "battery": battery,
        "threshold": threshold,
        "sufficient": battery >= threshold,
        "margin": battery - threshold,
    }


S11_BATTERY_ANALYSIS = ToolSpec(
    tool_id="S11",
    name="电量裕度分析",
    tool_class="S",
    executor_type="platform",
    input_schema={
        "type": "object",
        "required": ["battery"],
        "properties": {"battery": {"type": "number"}, "threshold": {"type": "number"}},
    },
    output_schema={"type": "object"},
    run=_battery_analysis,
)


C03_CLAIM = ToolSpec(
    tool_id="C03",
    name="申请资源",
    tool_class="C",
    executor_type="blackboard",
    aliases=["V02"],
    input_schema={
        "type": "object",
        "required": ["resource"],
        "properties": {"resource": {"type": "string"}},
    },
    output_schema={"type": "object"},
    run=None,
)


Y06_CAPABILITY_QUERY = ToolSpec(
    tool_id="Y06",
    name="设备能力查询",
    tool_class="Y",
    executor_type="registry",
    input_schema={"type": "object", "properties": {"device_id": {"type": "string"}}},
    output_schema={"type": "object"},
    run=None,
)
