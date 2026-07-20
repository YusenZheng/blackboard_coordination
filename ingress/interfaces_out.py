# STATUS: STAGED(A类,接口层)—— 三出向 Protocol 空实现(UI呈现层挂起,但接口层要钉)
"""ingress.interfaces_out —— 北向三个出向接口(Protocol)。

【C9】主心骨"必须先钉的接口"含三个出向。北向 UI 呈现层挂起(⏸),但接口层要钉——
做成 Protocol 不违反 UI 挂起,反而是解冻前的正确准备。别的层知道跟北向怎么接。

    situation_view —— 态势视图(← 黑板派生视图)
    authorization  —— 授权请求(← 安全平面可逆性门控 R1)
    estop          —— 急停(→ 安全平面急停回路,contracts.bypass.EmergencyStopBus)
"""
from __future__ import annotations

from typing import Protocol

from ..contracts.bypass import AuthDecision, EmergencyStop


class SituationView(Protocol):
    """态势视图出向:从黑板派生视图取数,推给大屏。UI 呈现层 TODO。"""
    def render(self, view: dict) -> None: ...


class AuthorizationView(Protocol):
    """授权请求出向:R1 动作的授权请求推给指挥官,收回决定。UI TODO。"""
    def request(self, intent_id: str, context: dict) -> None: ...
    def collect(self, intent_id: str) -> AuthDecision: ...


class EstopControl(Protocol):
    """急停出向:一键急停 → 安全平面急停回路(旁路软件)。UI TODO。"""
    def trigger(self, estop: EmergencyStop) -> None: ...
