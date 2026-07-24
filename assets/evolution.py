# STATUS: NOT-STAGED —— 自进化闭环本迭代不实现 / demo 只需 Skill 结构 + trigger 复用 / 待实现
"""assets.evolution —— 自进化闭环 + admission gate(准入门控)。

【本迭代不实现】自进化八步闭环(任务执行→复盘→经验抽象→候选生成→环境练习→验证评估
→灰度发布→版本回滚)+ 四层机制(经验抽象/群体Skill进化/Meta Skill/验证门控)+
admission gate 四关(指标/Probe/Trace回放/仿真/低风险试运行/人工审批/灰度)。

对标 AEROS:契约化技能准入 + fleet 联邦 + canary + committed-state 回滚。
最硬骨头:物理准入验证 handle(仿真 rollout + 真机 canary + 回滚)——数字域没现成的。

TODO:实现 admission gate;回灌接口(回灌到协同策略/
安全规则/Agent能力/工具接口)。
"""
from __future__ import annotations

from .skill import SkillCandidate


class AdmissionGate:
    """准入门控。NOT-STAGED:骨架只留形状。"""

    def evaluate(self, candidate: SkillCandidate) -> bool:
        """新 Skill 上真机前须过:指标核对/Probe/Trace回放/仿真/低风险试运行/人工审批/灰度。"""
        raise NotImplementedError(
            "NOT-STAGED: admission gate 待实现(物理准入验证 handle 是最硬骨头)")

    def rollback(self, skill_signature: str) -> None:
        """出问题回滚(committed-state rollback)。"""
        raise NotImplementedError("NOT-STAGED")
