# STATUS: STAGED(A类)—— MVP:三段 pipeline 空壳(intent→enrich→route),route 内含任务分流
"""ingress.task_gen —— 任务生成三段 pipeline。实现 contracts.interfaces.TaskGenPort。

【A5】不是一行写死,是三段 Agent Pipeline(对齐 v5 §5.2 + 补充说明交付项1):
    ① 意图理解 Agent:自然语言 → 结构化任务草案。
    ② 补全 Agent 群(EnrichmentAgent 接口位):各带记忆各补一块(宠物习性/场地禁区/天气窗口)。
       MVP 写死宠物/空间/天气三个 mock 补全。拆开是因为每块补全有出处、可积累、下次更准。
    ③ 路由 Agent:打三标签 [目标清晰度][环境开放度][风险等级] →
       (a) 任务分流:决定这单走哪种协同挡位(集中式 master-worker / 分布式自主应征 /
           预定义 workflow / 单机直控);
       (b) 初始信任等级(C6)。
    分流结果写进 TaskPackage.extra["mode"],协同层 ModeSelector.route 据此路由(北向定调、协同层执行)。

TODO:意图理解接真 LLM + 结构化输出校验;补全 Agent 群接六类预设虚拟 Agent
(个人/家庭/宠物/天气/空间/复盘);第③步三标签接真判定(现为占位规则)。
"""
from __future__ import annotations

import abc

from ..contracts.task import TaskPackage
from ..contracts.task_types import SEARCH_TARGET
from ..contracts.types import Position, TaskRequirement


class EnrichmentAgent(abc.ABC):
    """补全 Agent 接口位:各带记忆补一块。"""
    name: str = "enrich"

    @abc.abstractmethod
    def enrich(self, draft: dict) -> dict:
        """在任务草案上补一块(返回补充字段)。"""
        ...


class _MockPetAgent(EnrichmentAgent):
    name = "pet"
    def enrich(self, draft: dict) -> dict:
        return {"target_profile": {"species": "萨摩耶幼犬", "habit": "受惊往灌木/窄缝钻",
                                   "color": "白", "collar": "红"}}


class _MockSpaceAgent(EnrichmentAgent):
    name = "space"
    def enrich(self, draft: dict) -> dict:
        return {"space_constraints": [{"label": "gap_sample", "min_gap_cm": None,
                                       "passable_by": []}]}  # 具体尺寸由场景/仿真载入,任务生成不预设


class _MockWeatherAgent(EnrichmentAgent):
    name = "weather"
    def enrich(self, draft: dict) -> dict:
        return {"weather": {"condition": "晴", "window_ok": True}}


class TaskGen:
    def __init__(self):
        # 补全 Agent 群(MVP 三个 mock;真上接六类预设)
        self._enrichers = [_MockPetAgent(), _MockSpaceAgent(), _MockWeatherAgent()]
        self._seq = 0

    def generate(self, raw_input: str, media=None) -> TaskPackage:
        # ① 意图理解(MVP:识别"找X" → search_target)
        self._seq += 1
        draft = self._intent(raw_input, self._seq)
        # ② 补全 Agent 群
        for ea in self._enrichers:
            draft.update(ea.enrich(draft))
        # ③ 路由打标 → 挡位 + 初始信任等级
        # MVP 占位:开放环境找目标、需多机分工 → 分布式自主应征(autonomous);执行前人批(A1)。
        # TODO:接三标签([目标清晰度][环境开放度][风险等级])+ 在线设备数真判定,别用返回同值的假分支冒充。
        mode, autonomy = "autonomous", "A1"
        draft["mode"] = mode
        return TaskPackage(
            task_id=draft["task_id"], task_type=draft["task_type"], goal=draft["goal"],
            success_condition="目标被近距离确认",
            requirement=TaskRequirement(required_capabilities=["G01"], min_battery=0.2),
            target_profile=draft.get("target_profile", {}),
            area=Position(label="公园", area="公园"),
            priority="high", initial_autonomy_level=autonomy,
            extra={"mode": mode, "weather": draft.get("weather"),
                   "space_constraints": draft.get("space_constraints")})

    @staticmethod
    def _intent(raw: str, seq: int) -> dict:
        # MVP:写死"找狗";真上接 LLM。id 带序号避免多任务撞 id。
        return {"task_id": f"find_dog_{seq:03d}", "task_type": SEARCH_TARGET,
                "goal": raw or "帮我找一只走失的萨摩耶幼犬"}
