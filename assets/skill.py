# STATUS: STAGED(A类)—— MVP:七元组 Skill 结构 + L1/L2 分层 + trigger 匹配(复用已沉淀经验)
"""assets.skill —— 七元组 Skill + Skill Graph(结构定义)。

【C7】Skill 带 level(L1 组合 / L2 涌现)+ trigger(触发条件)+ tool_chain。
"签名"明确为 trigger 匹配键——匹配触发条件即命中已沉淀 Skill,省去重复现学(提速幅度待 L1 真计时验证)。

七元组(对齐关键技术一):签名 / 前提 / 执行策略 / 终止条件 / 证据 / 版本血缘 / 依赖偏序。
Skill 是经验参考,非编排脚本(守机制/策略分离)——模型检索到可参考,用不用它自己决策。

TODO:Skill Graph(依赖/冲突/组合/失效网络)、生命周期算子
(create/edit/merge/split/link/retire)、Representation Router、写时抽象。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SkillLevel(str, Enum):
    L1 = "L1"   # 组合技能(出厂成熟套路:扇形搜索分工/跨地形接力/物理约束匹配)
    L2 = "L2"   # 涌现技能(任务真长出来的:抢道让位钳形分工/边缘躲藏预判)


@dataclass
class Skill:
    """七元组 Skill。"""
    signature: str                      # 签名 = trigger 匹配键(检索命中用)
    preconditions: list = field(default_factory=list)   # 前提
    strategy: str = ""                  # 执行策略(参考,非脚本)
    termination: str = ""               # 终止条件
    evidence: list = field(default_factory=list)        # 证据(来自哪些 Trace)
    version_lineage: list = field(default_factory=list) # 版本血缘
    dependencies: list = field(default_factory=list)    # 依赖偏序
    # C7 三字段
    level: SkillLevel = SkillLevel.L1
    trigger: dict = field(default_factory=dict)         # 触发条件(如"两设备claim同一资源")
    tool_chain: list = field(default_factory=list)      # 涉及的 Tool 序列
    extra: dict = field(default_factory=dict)


@dataclass
class SkillCandidate:
    """技能候选(Y07 涌现触发产出,过 admission gate 才入库)。

    【按剧本校准】lifecycle_status 状态机:candidate → candidate_trial(受控试运行,
    如"变化密度感知"E19 先试跑) → validated(Y07 验证通过) → registered(Y08 注册)。
    """
    skill: Skill
    lifecycle_status: str = "candidate"   # candidate/candidate_trial/validated/registered
    discovery_source: list = field(default_factory=list)  # 产生候选的事件/执行轨迹
    source_trace_ids: list = field(default_factory=list)


class SkillGraph:
    """Skill Graph(MVP:名称索引 + trigger 匹配;真上做能力网络)。"""
    def __init__(self):
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.signature] = skill

    def match_by_situation(self, situation: str) -> Optional[Skill]:
        """按情境匹配(Agent Loop 决策前检索:命中则作经验参考)。"""
        for s in self._skills.values():
            if s.trigger.get("situation") == situation:
                return s
        return None

    def all(self) -> list:
        return list(self._skills.values())

    def search(
        self,
        *,
        task_type: str,
        situation_tags: list[str],
        capability_ids: list[str],
        available_tool_ids: set[str],
        limit: int = 3,
    ) -> list[Skill]:
        """按任务、情境、能力与当前可用 Tool 返回可执行的经验参考。"""
        query_tags = set(situation_tags)
        query_capabilities = set(capability_ids)
        matches: list[tuple[int, Skill]] = []
        for skill in self._skills.values():
            trigger = skill.trigger
            expected_task_type = trigger.get("task_type")
            if expected_task_type and expected_task_type != task_type:
                continue
            expected_situation = trigger.get("situation")
            if expected_situation and expected_situation not in query_tags:
                continue
            expected_capabilities = set(trigger.get("capability_ids", []))
            if expected_capabilities and not expected_capabilities <= query_capabilities:
                continue
            if skill.tool_chain and not set(skill.tool_chain) <= available_tool_ids:
                continue

            score = 0
            score += 4 if expected_task_type == task_type else 0
            score += 2 if expected_situation in query_tags else 0
            score += len(expected_capabilities & query_capabilities)
            matches.append((score, skill))
        matches.sort(key=lambda item: (-item[0], item[1].signature))
        return [skill for _, skill in matches[: max(0, limit)]]


def load_builtin_skills() -> SkillGraph:
    """加载内置 Skill(骨架只挂 1 样例;后续补的 Skill 也在这里挂)。"""
    g = SkillGraph()
    from .skills.fanshaped_search import FANSHAPED_SEARCH
    from .skills.safe_search_execution import SAFE_SEARCH_EXECUTION

    g.register(FANSHAPED_SEARCH)
    g.register(SAFE_SEARCH_EXECUTION)
    # TODO:照 fanshaped_search 补 多源线索定位/跨设备视觉接力/物理约束匹配/
    #   低电补位(L1)+ 抢道让位钳形分工/边缘躲藏预判(L2,过 Y07 验证注册)
    return g
