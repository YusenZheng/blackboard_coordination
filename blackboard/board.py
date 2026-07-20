# STATUS: STAGED(A类)—— MVP 可跑:内存 append-only + 四账本 + 租约 + 派生视图 + 防lost update占位
"""blackboard.board —— 共享黑板的 MVP 实现(内存 append-only)。

实现 contracts.interfaces.BlackboardPort。

【B2】黑板是协同事件唯一写入源;Trace 通过 subscribe 订阅派生落档,不双写。
【A12】subscribe 按异步 + ack/offset 语义设计,即使 MVP 是同步回调:
    - handler 收到 (event, offset);真上中间件时 at-least-once,handler 须幂等。
    - docstring 声明真依赖语义不变,避免协同层依赖同步回调、真上时全层改接口。
【地基钢筋】append 做幂等键去重 + 版本单调递增(防 lost update)。

TODO:
    - 换持久化存储(Redis Stream / Kafka / NATS / Postgres),subscribe 真异步 + 消费组 + offset。
    - 版本校验升级成 CAS(compare-and-swap),真正防并发 lost update。
    - 冲突三策略落地:状态类 last-write-wins / bid 类全 append 由协同层排序 / 关键资源用租约。
    - 平台重启 replay 事件流恢复在途租约和任务上下文。
"""
from __future__ import annotations

import itertools
from typing import Callable, Optional

from ..contracts.blackboard_event import BlackboardEvent, EventType, Ledger


class Blackboard:
    """内存 append-only 黑板。协同唯一介质。"""

    def __init__(self) -> None:
        self._events: list[BlackboardEvent] = []          # 事件流(append-only)
        self._seen_keys: set[str] = set()                 # 幂等键去重
        self._version = itertools.count(1)                # 版本单调递增
        self._offset = itertools.count(0)
        self._subscribers: list[tuple[str, Callable, Optional[list]]] = []
        self._leases: dict[str, str] = {}                 # 租约:resource -> device_id
        self._sub_id = itertools.count(1)

    # ── 写 ────────────────────────────────────────────────────────────
    def append(self, event: BlackboardEvent) -> int:
        """追加事件。幂等去重 + 版本递增。返回 offset。"""
        # 防 lost update 第一道:幂等键去重(MVP 版;真上用 CAS)
        if event.idempotency_key and event.idempotency_key in self._seen_keys:
            # 已见过,幂等跳过。返回当前最新 offset。
            return len(self._events) - 1
        if event.idempotency_key:
            self._seen_keys.add(event.idempotency_key)
        if event.version == 0:
            event.version = next(self._version)
        offset = next(self._offset)
        self._events.append(event)
        # 同步通知订阅者(MVP;真上异步)。handler 须幂等(at-least-once 语义)。
        for _sid, handler, types in self._subscribers:
            if types is None or event.type in types:
                handler(event, offset)
        return offset

    # ── 读 ────────────────────────────────────────────────────────────
    def read_events(self, since_offset: int = 0,
                    types: Optional[list] = None) -> list:
        evs = self._events[since_offset:]
        if types is not None:
            evs = [e for e in evs if e.type in types]
        return evs

    def query_view(self, ledger: Optional[Ledger] = None,
                   filt: Optional[dict] = None) -> dict:
        """派生视图:从事件流折叠出当前状态(见 view.py)。"""
        from .view import fold_view
        return fold_view(self._events, ledger=ledger, filt=filt)

    # ── 订阅(异步语义签名) ──────────────────────────────────────────
    def subscribe(self, handler: Callable[[BlackboardEvent, int], None],
                  types: Optional[list] = None, from_offset: int = 0) -> str:
        """订阅事件。handler(event, offset)。真上中间件 at-least-once,handler 须幂等。"""
        sid = f"sub-{next(self._sub_id)}"
        self._subscribers.append((sid, handler, types))
        # 补发历史(from_offset 起),让订阅者能追上
        for off, ev in enumerate(self._events):
            if off >= from_offset and (types is None or ev.type in types):
                handler(ev, off)
        return sid

    # ── 租约(关键资源独占:claim/yield) ────────────────────────────
    def claim(self, resource: str, device_id: str) -> bool:
        """claim 资源独占。重叠 claim 被拒。"""
        holder = self._leases.get(resource)
        if holder is None:
            self._leases[resource] = device_id
            return True
        return holder == device_id      # 已持有者重复 claim 视为成功,他人被拒

    def release(self, resource: str, device_id: str) -> None:
        """yield 释放租约(仅持有者能释放)。"""
        if self._leases.get(resource) == device_id:
            del self._leases[resource]
