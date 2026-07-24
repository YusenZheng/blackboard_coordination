"""Python logging 与当前 Trace 上下文的桥接。"""

from __future__ import annotations

import logging
import traceback
from typing import Any
from uuid import uuid4

from .context import CORRELATION_KEYS, correlation_context
from .redaction import redact_text, redact_value
from .storage import utc_now


class TraceContextFilter(logging.Filter):
    """为 LogRecord 自动附加 run/trace/span 及业务关联字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        context = correlation_context()
        for key in (
            "run_id",
            "trace_id",
            "span_id",
            "parent_span_id",
            *CORRELATION_KEYS,
        ):
            if not hasattr(record, key):
                setattr(record, key, context.get(key))
        return True


class SQLiteLogHandler(logging.Handler):
    """将结构化日志交给 Hub 的有界队列；``emit`` 永不抛向业务线程。"""

    def __init__(self, observer: Any, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.observer = observer
        self.addFilter(TraceContextFilter())
        self._swarm_observer_id = id(observer)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            context = correlation_context()
            for key in (
                "run_id",
                "trace_id",
                "span_id",
                "parent_span_id",
                *CORRELATION_KEYS,
            ):
                value = getattr(record, key, None)
                if value is not None:
                    context[key] = value
            exc_type: str | None = None
            stack_trace: str | None = None
            if record.exc_info:
                exc_type = record.exc_info[0].__name__
                stack_trace = redact_text(
                    "".join(traceback.format_exception(*record.exc_info))
                )
            elif record.exc_text:
                stack_trace = redact_text(record.exc_text)

            extra_attributes = getattr(record, "observation_attributes", {})
            payload = {
                "log_id": str(uuid4()),
                **context,
                "timestamp": utc_now(),
                "level": record.levelname.upper(),
                "module": record.name,
                "operation": getattr(record, "operation", None)
                or context.get("operation"),
                "message": redact_text(record.getMessage()),
                "error_type": getattr(record, "error_type", None) or exc_type,
                "stack_trace": stack_trace,
                "attributes": redact_value(extra_attributes),
            }
            self.observer._record_log(payload)
        except Exception:
            # logging.Handler.handleError 默认可能写 stderr；观测故障必须静默隔离。
            return
