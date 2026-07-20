# STATUS: STAGED(A类)—— MVP:mock 急停总线。真固件级急停/回路时序隔离后续实现
"""safety.estop —— 急停总线(A8 三条合法旁路之三)。实现 contracts.bypass.EmergencyStopBus。

【旁路语义】急停旁路所有软件层(Loop/网关/guardrail)直达设备驱动——软件可崩,急停不能崩
(OM1 固件级急停 + 安全回路时序隔离)。MVP 即使 print 也要有此原语,让旁路是设计不是漏洞。
事件仍留痕(往黑板 append ESTOP)。

TODO:真固件级急停信号、独立硬件回路、与 LLM 决策链时序隔离。
"""
from __future__ import annotations

from ..contracts.bypass import EmergencyStop


class MockEmergencyStopBus:
    def __init__(self, blackboard=None):
        self._bb = blackboard
        self._stopped: set = set()      # 被急停的 scope 集合("all" 或 device_id)

    def signal(self, estop: EmergencyStop) -> None:
        """急停:直达驱动、旁路所有软件层。置停状态 + 留痕。"""
        self._stopped.add(estop.scope)
        print(f"  [急停] 范围={estop.scope} 原因={estop.reason} "
              f"→ 直达设备驱动,旁路 Loop/网关/guardrail(软件可崩,急停不崩)")
        if self._bb is not None:
            from ..contracts.blackboard_event import BlackboardEvent, EventType, Ledger
            self._bb.append(BlackboardEvent(
                id=f"estop-{estop.scope}", type=EventType.ESTOP, ledger=Ledger.RECEIPT,
                content={"scope": estop.scope, "reason": estop.reason}, source="estop-bus"))

    def is_stopped(self, device_id: str) -> bool:
        """按 scope 分支寻址:全局急停 或 该设备被点名急停,都算停。"""
        return "all" in self._stopped or device_id in self._stopped

    def clear(self, scope: str = "all") -> None:
        """解除急停(A6:急停后要能恢复,否则结构上永远停着)。"""
        self._stopped.discard(scope)
        print(f"  [急停解除] 范围={scope}")
        if self._bb is not None:
            from ..contracts.blackboard_event import BlackboardEvent, EventType, Ledger
            self._bb.append(BlackboardEvent(
                id=f"estop-clear-{scope}", type=EventType.ESTOP, ledger=Ledger.RECEIPT,
                content={"scope": scope, "cleared": True}, source="estop-bus"))
