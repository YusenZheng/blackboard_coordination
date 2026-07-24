# STATUS: STAGED(A类)—— 适配器抽象接口(真机/仿真共享)
"""access.adapters.base —— 适配器抽象接口。实现 contracts.interfaces.AdapterPort。

【C15 做厚】不只 execute,还含契约动词 → 设备指令的翻译钩子 + 坐标/时间对齐钩子。
真机 adapter(drone/vehicle/dog)和仿真 adapter(sim.sim_adapter)都实现这个接口
——落实 v5 5.9"仿真与实机接口一致",真机没打通时 sim 顶同一角色。
"""
from __future__ import annotations

import abc

from ...contracts.task import ActionIntent, ActionReceipt
from ...contracts.verbs import ActionVerb


class BaseAdapter(abc.ABC):
    """设备适配器基类。子类实现具体协议(MAVLink/ROS2/SDK)或仿真。"""

    def __init__(self, device_id: str):
        self.device_id = device_id

    @abc.abstractmethod
    def execute(self, intent: ActionIntent) -> ActionReceipt:
        """执行动作意图。子类内部:契约动词 → 具体设备指令。"""
        ...

    @abc.abstractmethod
    def read_telemetry(self) -> dict:
        """读遥测(位姿/电量/状态)。"""
        ...

    # ── 做厚的钩子:契约动词 → 设备指令翻译(子类可覆盖) ──────────────
    def translate(self, verb: ActionVerb, params: dict) -> dict:
        """把契约动词翻译成本设备的指令 dict。默认透传,子类按协议覆盖。"""
        return {"verb": verb.value, "params": params}
