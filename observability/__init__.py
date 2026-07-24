"""V2 链路的非阻塞 Trace、日志和指标观测内核。

该包不依赖 runtime/coordination 等业务层。业务装配层只需注入
``ObservabilityHub``（或 ``NoopObservability``）即可。
"""

from .contracts import NoopObservability, ObservabilityPort, SpanHandle
from .hub import ObservabilityHub
from .logging_support import SQLiteLogHandler, TraceContextFilter
from .redaction import PayloadProcessor, redact_value
from .storage import SQLiteObservationStore

__all__ = [
    "NoopObservability",
    "ObservabilityHub",
    "ObservabilityPort",
    "PayloadProcessor",
    "SQLiteLogHandler",
    "SQLiteObservationStore",
    "SpanHandle",
    "TraceContextFilter",
    "redact_value",
]
