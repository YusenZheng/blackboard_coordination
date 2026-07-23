"""runtime —— coordination-v2 装配 + 正式 skeleton。

runtime 是唯一可以 import 所有层做装配的地方(其他层守依赖防火墙)。
"""

from .deepseek import (
    DeepSeekClient,
    DeepSeekGroupPlanningPolicy,
    DeepSeekIntentInterpreter,
    DeepSeekLocalProposalPolicy,
)
from .coordination_runtime import CoordinationRuntime, DeviceRuntimeConfig

__all__ = [
    "DeepSeekClient",
    "DeepSeekGroupPlanningPolicy",
    "DeepSeekIntentInterpreter",
    "DeepSeekLocalProposalPolicy",
    "CoordinationRuntime",
    "DeviceRuntimeConfig",
]
