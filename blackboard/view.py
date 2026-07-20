# STATUS: STAGED(A类)—— MVP 可跑:从事件流折叠出当前状态视图
"""blackboard.view —— 派生视图:把 append-only 事件流折叠成"当前状态"。

【核心】黑板两面:事件流(长期记忆,写=追加)vs 派生视图(工作记忆,读=折叠)。
智能体日常读的是派生视图(某任务现在归谁/某目标现在在哪),不读全部历史;
复盘/Trace 才读完整事件流。这就是"任务状态怎么随 append-only 变"的答案——
状态变化 = 追加新事件,视图 = 从事件流实时折叠出来的最新快照。

TODO:真上时视图可用物化视图/缓存增量更新,不必每次全量折叠。
"""
from __future__ import annotations

from typing import Optional

from ..contracts.blackboard_event import BlackboardEvent, EventType, Ledger


def fold_view(events: list, ledger: Optional[Ledger] = None,
              filt: Optional[dict] = None) -> dict:
    """折叠事件流成当前状态视图。

    MVP 折叠规则(够 skeleton 用;真上按四账本各自的折叠语义细化):
        - 任务:按 task_id 归集,状态取最后一条相关事件(posted→assigned→done)。
        - 证据:按 clue_id 归集。
        - 回执:按 intent_id 归集。
        - 人类确认:按 intent_id 归集授权请求/结果。
    """
    tasks: dict = {}
    evidence: dict = {}
    receipts: dict = {}
    human: dict = {}

    for e in events:
        if ledger is not None and e.ledger != ledger:
            continue
        c = e.content
        if e.type == EventType.TASK_POSTED:
            tasks[c.get("task_id")] = {"status": "posted", "owner": None, "content": c}
        elif e.type == EventType.TASK_ASSIGNED:
            t = tasks.setdefault(c.get("task_id"), {})
            t["status"] = "assigned"
            t["owner"] = c.get("device_id")
        elif e.type == EventType.TASK_DONE:
            t = tasks.setdefault(c.get("task_id"), {})
            t["status"] = "done"
        elif e.type == EventType.CLUE:
            evidence[c.get("clue_id")] = c
        elif e.type == EventType.RECEIPT:
            receipts[c.get("intent_id")] = c
        elif e.type in (EventType.AUTH_POINT, EventType.AUTH_DECISION):
            h = human.setdefault(c.get("intent_id"), {})
            h[e.type.value] = c

    return {"tasks": tasks, "evidence": evidence,
            "receipts": receipts, "human": human}
