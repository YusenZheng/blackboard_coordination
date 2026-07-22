# STATUS: STAGED(A类)—— 协同输出的 durable outbox 发布器
"""Outbox-first Blackboard 发布逻辑。"""
from __future__ import annotations

from typing import Callable, Optional

from ..contracts.blackboard_event import BlackboardEvent
from .models import AppendResult, AppendStatus, enum_value
from .ports import BlackboardClientPort


class DurableEventPublisher:
    def __init__(
        self,
        blackboard: BlackboardClientPort,
        stage: Callable[[BlackboardEvent], object],
        resolve: Callable[[str], None],
        reject: Optional[Callable[[str, AppendResult], None]] = None,
        *,
        auto_resolve_success: bool = True,
    ) -> None:
        self.blackboard = blackboard
        self._stage = stage
        self._resolve = resolve
        self._reject = reject
        self.auto_resolve_success = auto_resolve_success

    def publish(self, event: BlackboardEvent) -> AppendResult:
        if not event.idempotency_key:
            raise ValueError("coordination output requires idempotency_key")
        staged = self._stage(event)
        # Store 返回第一次持久化的 canonical event。发生崩溃重放时，即使调用方
        # 重新计算出了不同的时间字段，也必须提交旧的稳定对象。
        canonical_event = staged if isinstance(staged, BlackboardEvent) else event
        result = normalize_append_result(
            self.blackboard.append(canonical_event), canonical_event.id
        )
        if result.status in (AppendStatus.APPENDED, AppendStatus.DUPLICATE_SAME):
            if self.auto_resolve_success:
                self._resolve(event.idempotency_key)
        elif result.status == AppendStatus.REJECTED:
            if self._reject is not None:
                self._reject(event.idempotency_key, result)
            raise PermanentAppendError(result)
        elif result.status == AppendStatus.KEY_CONFLICT:
            raise AppendConflictError(result)
        # terminal_conflict 需要调用方读取 TerminalView 后再决定是否清理 Outbox。
        return result

    def resolve(self, idempotency_key: str) -> None:
        self._resolve(idempotency_key)


class AppendError(RuntimeError):
    def __init__(self, result: AppendResult) -> None:
        super().__init__(f"append failed: {result.status.value}:{result.reason_code}")
        self.result = result


class PermanentAppendError(AppendError):
    pass


class AppendConflictError(AppendError):
    pass


def normalize_append_result(value: object, event_id: str) -> AppendResult:
    if isinstance(value, AppendResult):
        return value
    status_value = getattr(value, "status", None)
    if status_value is None:
        raise TypeError("Blackboard append must return AppendResult, not a legacy offset")
    status = AppendStatus(enum_value(status_value))
    return AppendResult(
        status=status,
        offset=getattr(value, "offset", None),
        version=getattr(value, "version", None),
        event_id=str(getattr(value, "event_id", event_id)),
        existing_event_id=getattr(value, "existing_event_id", None),
        reason_code=getattr(value, "reason_code", None),
    )
