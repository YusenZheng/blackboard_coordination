# STATUS: STAGED(A类)—— 枚举+直通函数已立;接线点=guardrail.check(接线点待接:高于声明等级的动作需降级/授权)
"""safety.autonomy —— 信任自治等级 A0-A4(人参与多深)。

对齐 v5 支柱四"信用式自治":等级靠履历挣出来。按任务类型/设备/区域/时段设。

TODO:
    - policy 引擎:按 (任务类型, 设备, 区域, 时段, 风险) 查等级。
    - 信用式自治:autonomy 读 registry 的 resume,给"这台设备该类任务可升 A2→A3"的建议。
"""
from __future__ import annotations

from enum import Enum


class AutonomyLevel(str, Enum):
    """信任自治等级(A 字母,与可逆性 R 档错开命名)。"""
    A0 = "A0"   # 建议:平台只出方案,人来派单
    A1 = "A1"   # 确认:平台自动组队,执行前人批准
    A2 = "A2"   # 监督:自主执行,关键动作需确认,人可随时接管
    A3 = "A3"   # 例外:自主执行,仅异常升级到人
    A4 = "A4"   # 委托:跨任务长期授权


def policy_for(task_type: str, device_id: str, area: str = "",
               declared: str = "A0") -> AutonomyLevel:
    """返回该任务/设备/区域的信任等级。

    MVP:直接采用任务包声明的 initial_autonomy_level(C6:由 ingress 路由打标给出)。
    TODO:接真 policy 表 + 履历驱动的升级建议。
    """
    try:
        return AutonomyLevel(declared)
    except ValueError:
        return AutonomyLevel.A0
