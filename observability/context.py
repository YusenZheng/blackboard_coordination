"""跨 Trace、logging 和 SQLite 记录共享的轻量关联上下文。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Mapping


CURRENT_RUN_ID: ContextVar[str] = ContextVar("observation_run_id", default="")
CURRENT_TRACE_ID: ContextVar[str] = ContextVar("observation_trace_id", default="")
CURRENT_SPAN_ID: ContextVar[str] = ContextVar("observation_span_id", default="")
CURRENT_PARENT_SPAN_ID: ContextVar[str] = ContextVar(
    "observation_parent_span_id", default=""
)
CURRENT_FIELDS: ContextVar[dict[str, Any]] = ContextVar(
    "observation_fields", default={}
)

CORRELATION_KEYS = (
    "task_id",
    "task_revision",
    "coordination_epoch",
    "agent_id",
    "device_id",
    "intent_id",
    "call_id",
    "event_id",
    "offset",
    "operation",
)


def correlation_context() -> dict[str, Any]:
    """返回当前上下文的扁平副本，供日志和事件自动关联。"""

    result: dict[str, Any] = {
        "run_id": CURRENT_RUN_ID.get(),
        "trace_id": CURRENT_TRACE_ID.get(),
        "span_id": CURRENT_SPAN_ID.get(),
        "parent_span_id": CURRENT_PARENT_SPAN_ID.get(),
    }
    result.update(CURRENT_FIELDS.get())
    return {key: value for key, value in result.items() if value not in ("", None)}


@contextmanager
def bind_fields(fields: Mapping[str, Any] | None = None) -> Iterator[None]:
    """临时叠加结构化关联字段。"""

    merged = dict(CURRENT_FIELDS.get())
    if fields:
        merged.update(
            {
                key: value
                for key, value in fields.items()
                if key in CORRELATION_KEYS and value is not None
            }
        )
    token = CURRENT_FIELDS.set(merged)
    try:
        yield
    finally:
        CURRENT_FIELDS.reset(token)
