# STATUS: STAGED(A类)—— MVP:招投标 bid 排序 + claim/yield 租约 + 仲裁入口占位
"""coordination.conflict —— 冲突分治。实现 contracts.interfaces.ConflictPort。

【C4】拆两类角色:
    - 中性框架服务(本文件):租约管理器 + bid 排序器 —— 无 LLM、非 Agent、无 Card。
    - 仲裁 Agent(僵局/C类分歧才动用)—— arbitrate() 是它的入口。
【判据】资源仲裁 ≠ 中央调度:只裁资源归属,不生成任务分解、不指派动作。

冲突四类分治(确定性能解决的不丢给大模型):
    A 任务归属 → 招投标(rank_bids,按 bid 出价)
    B 物理资源 → 租约独占(claim/release)+ 僵局仲裁入口(arbitrate,MVP 先申请先得,三层优先级 TODO)
    C 方案分歧 → 有限轮 debate + 加权投票(TODO)
    安全冲突 → 规则/人工兜底(safety 层)
"""
from __future__ import annotations

from ..contracts.types import ParticipationDecision


class ConflictService:
    """中性框架服务(租约管理器 + bid 排序器)。持有黑板做租约。"""

    def __init__(self, blackboard):
        self._bb = blackboard

    # A 类:招投标
    def rank_bids(self, task_id: str, decisions: list) -> ParticipationDecision:
        """对 list[ParticipationDecision] 排序,返回中标者(bid_score 最高的 accept 者)。

        设备之间不"辩论"——谁电量够、离得近、载重足是算出来的,不是吵出来的。
        """
        accepts = [d for d in decisions if d.accept]
        if not accepts:
            return None
        return max(accepts, key=lambda d: d.bid_score)

    # B 类:租约独占
    def claim(self, resource: str, device_id: str) -> bool:
        return self._bb.claim(resource, device_id)

    def release(self, resource: str, device_id: str) -> None:
        self._bb.release(resource, device_id)

    # B 类僵局:仲裁(仲裁 Agent 入口)
    def arbitrate(self, resource: str, contenders: list) -> str:
        """僵局仲裁入口。MVP:先申请先得(取 request_ts 最早者),够演"僵局有裁决"。

        contenders: list[dict],每个含 {device_id, safety_rank, capability_rank, request_ts}。
        返回胜出 device_id。
        TODO:三层优先级 P1 安全 > P2 能力/权限 > P3 时间 的真判定(有唯一正确答案,不靠讨论/投票)。
        """
        if not contenders:
            return None
        best = min(contenders, key=lambda c: c.get("request_ts", 0.0))
        return best["device_id"]

    # C 类:方案分歧
    def debate(self, topic: str, proposals: list) -> dict:
        """有限轮 debate + 加权投票。TODO:≤2 轮,超时上抛仲裁;
        票权重(安全>先验>省电)作为可配置,不硬编码(B3 拍板:投票权重是可覆盖配置)。"""
        raise NotImplementedError("NOT-STAGED: C 类方案分歧 debate 待实现")
