# STATUS: STAGED(A类)—— MVP 可跑:内存 registry,设备注册/发现(能力硬过滤)/刷新/履历更新
"""access.registry —— Agent 化注册处。实现 contracts.interfaces.RegistryPort。

设备接入 → 注册成一个 Agent(生成 AgentCard)。能力可注册/发现/刷新——
架构必守"可注册、可发现、可调用、能力变化可刷新",协议(MCP 等)留下面。

C14:动作回执 → resume 履历更新 → autonomy 读 resume 给等级建议(闭环接口位)。

TODO:
    - 自注册协议:每台设备 = 一个自注册 MCP server(或选型),tools/list 拉能力写注册表,
      listChanged 热更新。新设备接入 ≤30 分钟、断线重连 ≤30 秒恢复身份。
    - 能力声明进 tool schema(载重/续航/可达作为元数据,上层选工具时硬过滤)。
"""
from __future__ import annotations

import time
from typing import Optional

from ..contracts.agent_card import AgentCard
from ..contracts.types import DeviceCandidate, TaskRequirement


class Registry:
    """内存注册表(MVP)。"""

    def __init__(self, *, emit_console: bool = True) -> None:
        self._cards: dict[str, AgentCard] = {}
        self._emit_console = emit_console

    def register(self, card: AgentCard) -> None:
        card.extra["card_version"] = max(1, int(card.extra.get("card_version", 0)))
        card.state.extra["state_updated_at"] = float(
            card.state.extra.get("state_updated_at") or time.time()
        )
        self._cards[card.identity.device_id] = card
        if self._emit_console:
            print(f"  [注册] 设备 {card.identity.device_id}"
                  f"({card.identity.device_type.value}) → Agent 化,能力"
                  f"{card.capability.atomic_tools}")

    def refresh(self, device_id: str, card: AgentCard) -> None:
        """能力变化(挂新载荷)时刷新(listChanged 抽象)。"""
        previous = self._cards.get(device_id)
        previous_version = (
            int(previous.extra.get("card_version", 0)) if previous is not None else 0
        )
        card.extra["card_version"] = previous_version + 1
        card.state.extra["state_updated_at"] = float(
            card.state.extra.get("state_updated_at") or time.time()
        )
        self._cards[device_id] = card

    def get(self, device_id: str) -> Optional[AgentCard]:
        return self._cards.get(device_id)

    def all_cards(self) -> list:
        return list(self._cards.values())

    def discover(self, requirement: Optional[TaskRequirement] = None) -> list:
        """发现候选设备(能力硬过滤)。返回 list[DeviceCandidate]。"""
        out = []
        for card in self._cards.values():
            if requirement and not self._matches(card, requirement):
                continue
            out.append(DeviceCandidate(
                device=card.identity, state=card.state, capability=card.capability.profile))
        return out

    @staticmethod
    def _matches(card: AgentCard, req: TaskRequirement) -> bool:
        """bid 两段式第一段:规则硬过滤(电量/能力)。不过直接不进候选。

        TODO(接缝·命名词表未统一):这里拿 req.required_capabilities 比 card.capability.atomic_tools
            (Tool id,如 G01)。当前 task_gen 生成的 required_capabilities 也用 G01,故碰巧能过;
            但 CapabilityProfile.capabilities 是另一套名(如 ground_search),contracts.types 文档
            又说 required_capabilities 应是能力名 —— 三套命名并存,靠"生产/消费两头同用 G01"维持假一致。
            词表锁哪一套(能力名 or Tool id)由总监拍定,再统一 Card 三栏 / TaskRequirement / 本比对。
        """
        if card.state.battery < req.min_battery:
            return False
        cap_tools = set(card.capability.atomic_tools)
        if req.required_capabilities and not set(req.required_capabilities) <= cap_tools:
            return False
        return True

    def update_resume(self, device_id: str, success: bool) -> None:
        """C14:动作回执 → 履历更新(MVP:成功率统计)。"""
        card = self._cards.get(device_id)
        if not card:
            return
        card.resume.tasks_total += 1
        if success:
            card.resume.tasks_done += 1
        card.extra["card_version"] = int(card.extra.get("card_version", 1)) + 1
