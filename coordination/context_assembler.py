# STATUS: STAGED(A类)—— MVP:装配决策点上下文形状;Budget 接口位
"""coordination.context_assembler —— 上下文装配(Harness 横切能力)。

【C13:真工程量在这里,9-10月主战场】主心骨"每次判断前装配";照抄补充说明 2.4
"决策点 → 字段"映射表。不做的话 5 个 loop 各写一遍装配逻辑。

两样(MVP 接口位):
    - context 装配:按决策点(要不要应征/让位/接力/蹲点)取黑板/设备状态/线索/履历字段。
    - Budget(battery, time_window):预算是电量+时间窗,不是 token/轮次(v5 §5.3)。

TODO:按 2.4 表逐个决策点实现装配(取黑板视图/设备状态/线索/履历);token 预算工程
(单次任务理解 ≤4000);低置信线索/无关设备裁剪;履历注入。多模型路由(v5 §5.8)后续实现。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Budget:
    """预算:电量 + 时间窗(非 token 计数)。电量见底强制 return_home。"""
    battery: float = 1.0
    time_window_s: float = 0.0


class ContextAssembler:
    def assemble(self, decision_point: str, blackboard, device_card,
                 budget: Budget = None) -> dict:
        """按决策点装配上下文。MVP:返回决策点形状(真装配见 TODO)。"""
        # TODO:按 2.4 决策点→字段表真装配(黑板视图 + 自身状态 + 线索 + 履历 + 预算)。
        return {"decision_point": decision_point}
