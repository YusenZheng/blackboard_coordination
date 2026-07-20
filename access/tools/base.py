# STATUS: STAGED(A类,样板)—— Tool 定义形状 + 注册表
"""access.tools.base —— Tool 定义(ToolSpec)+ ToolRegistry。

ToolSpec = 一个 Tool 的稳定定义:id / 名称 / 所属类 / IO schema(对齐 IO 设计)/ 执行函数。
ToolRegistry = 启动时登记所有 Tool;tool_gateway.call_tool 从这里查。

【这就是"Tool 有地方放、能被登记、能被调"的落点。】
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ToolSpec:
    """一个 Tool 的稳定定义。IO schema 用 dict 描述字段(骨架够用;真上可换 pydantic)。"""
    tool_id: str                     # 如 "G01"
    name: str                        # 如 "定点导航"
    tool_class: str                  # G / S / C / V / Y
    input_schema: dict = field(default_factory=dict)    # {字段名: 类型说明}
    output_schema: dict = field(default_factory=dict)
    reuse_events: list = field(default_factory=list)     # 复用的事件(如 ["E05","E12"])
    run: Optional[Callable] = None   # 执行函数(mock 或真实现)
    extra: dict = field(default_factory=dict)


class ToolRegistry:
    """Tool 注册表。启动时各 Tool 注册进来,tool_gateway 从这里查。"""

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.tool_id] = spec

    def get(self, tool_id: str) -> Optional[ToolSpec]:
        return self._tools.get(tool_id)


def load_builtin_tools() -> ToolRegistry:
    """加载内置 Tool(骨架只挂 1 个样例;后续补的 Tool 也在这里挂)。"""
    reg = ToolRegistry()
    from .g01_navigate import G01_NAVIGATE
    reg.register(G01_NAVIGATE)
    # TODO:reg.register(G02_SCAN) ... 照 g01_navigate 样例逐个补
    return reg
