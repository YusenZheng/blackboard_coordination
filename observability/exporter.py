"""把官方 OpenTelemetry ReadableSpan 导出到本地 SQLite 写队列。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from .storage import SQLiteObservationStore, utc_now

try:  # 依赖由运行环境显式安装；缺失时 Hub 使用兼容降级 Span。
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    OTEL_EXPORTER_AVAILABLE = True
except ImportError:  # pragma: no cover - 是否安装由环境决定
    SpanExporter = object  # type: ignore[assignment,misc]

    class _FallbackResult:
        SUCCESS = "SUCCESS"
        FAILURE = "FAILURE"

    SpanExportResult = _FallbackResult  # type: ignore[assignment,misc]
    OTEL_EXPORTER_AVAILABLE = False


def _hex_trace_id(value: int | None) -> str:
    return f"{value:032x}" if value else ""


def _hex_span_id(value: int | None) -> str:
    return f"{value:016x}" if value else ""


def _time_from_ns(value: int | None) -> str:
    if not value:
        return utc_now()
    return datetime.fromtimestamp(
        value / 1_000_000_000, tz=timezone.utc
    ).isoformat(timespec="milliseconds")


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


class SQLiteSpanExporter(SpanExporter):  # type: ignore[misc]
    """非阻塞 exporter：这里只入队，不在 OTel worker 内执行 SQLite I/O。"""

    def __init__(self, store: SQLiteObservationStore) -> None:
        self.store = store

    def export(self, spans: Iterable[Any]) -> Any:
        for span in spans:
            context = span.context
            parent = span.parent
            attributes = dict(span.attributes or {})
            status_code = getattr(
                getattr(span.status, "status_code", None), "name", "UNSET"
            )
            record = {
                "span_id": _hex_span_id(getattr(context, "span_id", 0)),
                "trace_id": _hex_trace_id(getattr(context, "trace_id", 0)),
                "parent_span_id": _hex_span_id(
                    getattr(parent, "span_id", 0) if parent else 0
                )
                or None,
                "run_id": attributes.get("swarm.run.id"),
                "task_id": attributes.get("task.id")
                or attributes.get("task_id"),
                "name": span.name,
                "kind": getattr(span.kind, "name", str(span.kind)),
                "start_time": _time_from_ns(span.start_time),
                "end_time": _time_from_ns(span.end_time),
                "duration_ms": max(
                    0.0, (span.end_time - span.start_time) / 1_000_000
                ),
                "status_code": status_code,
                "status_message": getattr(span.status, "description", None),
                "attributes": _plain(attributes),
                "events": [
                    {
                        "name": event.name,
                        "timestamp": _time_from_ns(event.timestamp),
                        "attributes": _plain(dict(event.attributes or {})),
                    }
                    for event in span.events
                ],
                "links": [
                    {
                        "trace_id": _hex_trace_id(link.context.trace_id),
                        "span_id": _hex_span_id(link.context.span_id),
                        "attributes": _plain(dict(link.attributes or {})),
                    }
                    for link in span.links
                ],
                "input_payload_id": attributes.get(
                    "swarm.payload.input_id"
                ),
                "output_payload_id": attributes.get(
                    "swarm.payload.output_id"
                ),
                "created_at": utc_now(),
            }
            priority = (
                "critical"
                if status_code == "ERROR"
                or span.name
                in {
                    "swarm_brain.chain1.run",
                    "blackboard.event.append",
                    "safety.pre_check",
                    "safety.post_check",
                }
                else "normal"
            )
            self.store.enqueue("span", record, priority=priority)
        # 丢记录由 store.health 暴露；不能让 exporter 失败反向干扰业务。
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True
