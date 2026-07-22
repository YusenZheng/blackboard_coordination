# STATUS: STAGED(A类)—— 协同侧 Blackboard 合同守卫，不实现黑板存储
"""Blackboard 客户端防腐层。

本类不把旧内存 Blackboard 模拟成 v2 权威服务；它只校验外部 backend 是否遵守
05/06 已固定的 append、cursor 和 view watermark 合同。
"""
from __future__ import annotations

from typing import Any, Optional

from ..contracts.blackboard_event import BlackboardEvent
from .models import AppendResult, EventEnvelope, event_type_value
from .publisher import normalize_append_result


class BlackboardContractError(RuntimeError):
    pass


class ViewNotCaughtUpError(BlackboardContractError):
    def __init__(self, required_version: int, current_view_version: int) -> None:
        super().__init__(
            f"VIEW_NOT_CAUGHT_UP required={required_version} current={current_view_version}"
        )
        self.reason_code = "VIEW_NOT_CAUGHT_UP"
        self.required_version = required_version
        self.current_view_version = current_view_version
        self.retryable = True


class BlackboardClient:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def now(self) -> float:
        value = float(self.backend.now())
        if value < 0:
            raise BlackboardContractError("Blackboard now() returned a negative timestamp")
        return value

    def high_watermark(self) -> int:
        value = int(self.backend.high_watermark())
        if value < 0:
            raise BlackboardContractError("Blackboard high_watermark() must be non-negative")
        return value

    def append(self, event: BlackboardEvent) -> AppendResult:
        return normalize_append_result(self.backend.append(event), event.id)

    def read_since(
        self,
        offset: int,
        event_types: Optional[set[str]],
        limit: int,
        timeout_s: float,
    ) -> list[EventEnvelope]:
        if offset < -1:
            raise ValueError("last_offset must be >= -1")
        if limit <= 0:
            raise ValueError("limit must be positive")
        if timeout_s < 0:
            raise ValueError("timeout_s must be non-negative")
        raw = self.backend.read_since(offset, event_types, limit, timeout_s)
        envelopes = [_normalize_envelope(value) for value in raw]
        if len(envelopes) > limit:
            raise BlackboardContractError("read_since returned more than limit")
        previous = offset
        for envelope in envelopes:
            if envelope.offset <= previous:
                raise BlackboardContractError(
                    "read_since offsets must be strictly increasing and greater than last_offset"
                )
            if event_types is not None and event_type_value(envelope.event) not in event_types:
                raise BlackboardContractError("read_since returned an unrequested event type")
            previous = envelope.offset
        return envelopes

    def query_view(
        self,
        ledger: Optional[str],
        filt: dict,
        min_version: Optional[int],
        timeout_s: float,
    ) -> dict | list[dict]:
        value = self.backend.query_view(ledger, filt, min_version, timeout_s)
        if min_version is None:
            return value
        current = _view_watermark(value)
        if current < min_version:
            raise ViewNotCaughtUpError(min_version, current)
        return value


def _normalize_envelope(value: Any) -> EventEnvelope:
    if isinstance(value, EventEnvelope):
        return value
    if isinstance(value, dict):
        return EventEnvelope(offset=int(value["offset"]), event=value["event"])
    return EventEnvelope(offset=int(value.offset), event=value.event)


def _view_watermark(value: Any) -> int:
    if isinstance(value, dict):
        if "view_version" in value:
            return int(value["view_version"])
        children = [
            int(item["view_version"])
            for item in value.values()
            if isinstance(item, dict) and "view_version" in item
        ]
        return min(children) if children else -1
    if isinstance(value, list):
        children = [
            int(item["view_version"])
            for item in value
            if isinstance(item, dict) and "view_version" in item
        ]
        return min(children) if children else -1
    return -1
