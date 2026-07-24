"""OpenTelemetry 兼容的观测门面。"""

from __future__ import annotations

import atexit
import json
import logging
import re
import threading
import time
import traceback as traceback_module
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .context import (
    CORRELATION_KEYS,
    CURRENT_FIELDS,
    CURRENT_PARENT_SPAN_ID,
    CURRENT_RUN_ID,
    CURRENT_SPAN_ID,
    CURRENT_TRACE_ID,
    correlation_context,
)
from .exporter import SQLiteSpanExporter
from .logging_support import SQLiteLogHandler
from .redaction import PayloadProcessor, redact_text, redact_value
from .storage import DEFAULT_DB_PATH, SQLiteObservationStore, utc_now

try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.context import attach as otel_attach
    from opentelemetry.context import detach as otel_detach
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import (
        Link,
        NonRecordingSpan,
        SpanContext,
        SpanKind,
        Status,
        StatusCode,
        TraceFlags,
        TraceState,
    )
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    OTEL_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - 由部署环境是否安装依赖决定
    OTEL_SDK_AVAILABLE = False


_TRACEPARENT = re.compile(
    r"^(?:00)-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$",
    re.IGNORECASE,
)
_CRITICAL_EVENTS = {
    "run.end",
    "run.failed",
    "blackboard.event.append",
    "blackboard.commit",
    "safety.intercept",
    "safety.pre_check",
    "safety.post_check",
}
_HIGH_CARDINALITY_METRIC_KEYS = {
    "run_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "task_id",
    "agent_id",
    "device_id",
    "intent_id",
    "event_id",
    "call_id",
    "payload_id",
    "request_id",
    "offset",
}


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    )


def _safe_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(redact_value(dict(attributes or {})))


def _otel_attribute(value: Any) -> Any:
    safe = redact_value(value)
    if isinstance(safe, (str, bool, int, float)):
        return safe
    if isinstance(safe, list) and all(
        isinstance(item, (str, bool, int, float)) for item in safe
    ):
        return safe
    return json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )[:4096]


def _otel_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        str(key): _otel_attribute(value)
        for key, value in (attributes or {}).items()
        if value is not None
    }


def _business_fields(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    attrs = dict(attributes or {})
    aliases = {
        "task.id": "task_id",
        "task.revision": "task_revision",
        "coordination.epoch": "coordination_epoch",
        "agent.id": "agent_id",
        "device.id": "device_id",
        "intent.id": "intent_id",
        "tool.call.id": "call_id",
        "event.id": "event_id",
        "event.offset": "offset",
        "blackboard.offset": "offset",
    }
    fields: dict[str, Any] = {}
    for key in CORRELATION_KEYS:
        if key in attrs and attrs[key] is not None:
            fields[key] = attrs[key]
    for source, target in aliases.items():
        if source in attrs and attrs[source] is not None:
            fields[target] = attrs[source]
    return fields


def _normalize_link(value: Any) -> dict[str, str] | None:
    if isinstance(value, Mapping):
        trace_id = str(value.get("trace_id") or "")
        span_id = str(value.get("span_id") or "")
        if not trace_id and "traceparent" in value:
            match = _TRACEPARENT.match(str(value["traceparent"]))
            if match:
                trace_id, span_id = match.group(1), match.group(2)
        if re.fullmatch(r"[0-9a-fA-F]{32}", trace_id) and re.fullmatch(
            r"[0-9a-fA-F]{16}", span_id
        ):
            return {
                "trace_id": trace_id.lower(),
                "span_id": span_id.lower(),
                "trace_flags": str(value.get("trace_flags") or "01"),
            }
    if OTEL_SDK_AVAILABLE:
        if isinstance(value, SpanContext):
            return {
                "trace_id": f"{value.trace_id:032x}",
                "span_id": f"{value.span_id:016x}",
                "trace_flags": f"{int(value.trace_flags):02x}",
            }
        try:
            span = otel_trace.get_current_span(value)
            context = span.get_span_context()
            if context.is_valid:
                return {
                    "trace_id": f"{context.trace_id:032x}",
                    "span_id": f"{context.span_id:016x}",
                    "trace_flags": f"{int(context.trace_flags):02x}",
                }
        except (AttributeError, TypeError):
            pass
    return None


class _BaseSpanHandle(AbstractContextManager["_BaseSpanHandle"]):
    def __init__(
        self,
        hub: "ObservabilityHub",
        name: str,
        attributes: Mapping[str, Any] | None,
        links: list[Any] | None,
        input_payload: Any,
        context: Any,
        kind: str,
    ) -> None:
        self.hub = hub
        self.name = name
        self.attributes = _safe_attributes(attributes)
        self.links = links or []
        self.input_payload = input_payload
        self.output_payload: Any = None
        self.context = context
        self.kind = kind
        self._input_captured = False
        self._output_captured = False
        self._exception_recorded = False
        self._run_id = CURRENT_RUN_ID.get()
        self._trace_id = ""
        self._span_id = ""
        self._parent_span_id = ""
        self._trace_flags = "01"
        self._tokens: list[tuple[Any, Any]] = []

    @property
    def span_context(self) -> Mapping[str, str]:
        if not self._trace_id or not self._span_id:
            return {}
        return {
            "trace_id": self._trace_id,
            "span_id": self._span_id,
            "trace_flags": self._trace_flags,
            "traceparent": (
                f"00-{self._trace_id}-{self._span_id}-{self._trace_flags}"
            ),
        }

    def _bind_context(self) -> None:
        self._tokens = [
            (CURRENT_RUN_ID, CURRENT_RUN_ID.set(self._run_id)),
            (CURRENT_TRACE_ID, CURRENT_TRACE_ID.set(self._trace_id)),
            (CURRENT_SPAN_ID, CURRENT_SPAN_ID.set(self._span_id)),
            (
                CURRENT_PARENT_SPAN_ID,
                CURRENT_PARENT_SPAN_ID.set(self._parent_span_id),
            ),
            (
                CURRENT_FIELDS,
                CURRENT_FIELDS.set(
                    {**CURRENT_FIELDS.get(), **_business_fields(self.attributes)}
                ),
            ),
        ]

    def _unbind_context(self) -> None:
        for variable, token in reversed(self._tokens):
            variable.reset(token)
        self._tokens = []

    def _capture_input(self, is_error: bool = False) -> str | None:
        if self.input_payload is None or self._input_captured:
            return None
        payload_id = self.hub._capture_payload(
            self.input_payload,
            direction="input",
            is_error=is_error,
            run_id=self._run_id,
            trace_id=self._trace_id,
            span_id=self._span_id,
        )
        if payload_id:
            self._input_captured = True
        return payload_id

    def _capture_output(self, is_error: bool = False) -> str | None:
        if self.output_payload is None or self._output_captured:
            return None
        payload_id = self.hub._capture_payload(
            self.output_payload,
            direction="output",
            is_error=is_error,
            run_id=self._run_id,
            trace_id=self._trace_id,
            span_id=self._span_id,
        )
        if payload_id:
            self._output_captured = True
        return payload_id

    def set_output(self, payload: Any) -> None:
        self.output_payload = payload


class _OtelSpanHandle(_BaseSpanHandle):
    def __enter__(self) -> "_OtelSpanHandle":
        span_kind = getattr(SpanKind, self.kind.upper(), SpanKind.INTERNAL)
        link_values: list[Any] = []
        for candidate in self.links:
            link = self.hub._to_otel_link(candidate)
            if link is not None:
                link_values.append(link)
        parent_context = self.context
        current_before = otel_trace.get_current_span()
        parent_before = current_before.get_span_context()
        if parent_context is not None:
            try:
                explicit_parent = otel_trace.get_current_span(
                    parent_context
                ).get_span_context()
                if explicit_parent.is_valid:
                    parent_before = explicit_parent
            except (AttributeError, TypeError):
                pass
        self._manager = self.hub._tracer.start_as_current_span(
            self.name,
            context=parent_context,
            kind=span_kind,
            attributes=_otel_attributes(
                {
                    **self.attributes,
                    "swarm.run.id": self._run_id or None,
                }
            ),
            links=link_values,
            record_exception=False,
            set_status_on_exception=False,
        )
        self._span = self._manager.__enter__()
        context = self._span.get_span_context()
        self._trace_id = f"{context.trace_id:032x}"
        self._span_id = f"{context.span_id:016x}"
        self._trace_flags = f"{int(context.trace_flags):02x}"
        explicit_parent = getattr(self._span, "parent", None)
        if explicit_parent and getattr(explicit_parent, "is_valid", False):
            self._parent_span_id = f"{explicit_parent.span_id:016x}"
        elif parent_before.is_valid:
            self._parent_span_id = f"{parent_before.span_id:016x}"
        self._bind_context()
        if self._run_id and not self._parent_span_id:
            self.hub._note_trace(self._run_id, self._trace_id)
        input_id = self._capture_input()
        if input_id:
            self._span.set_attribute("swarm.payload.input_id", input_id)
        return self

    def set_output(self, payload: Any) -> None:
        super().set_output(payload)
        output_id = self._capture_output()
        if output_id:
            self._span.set_attribute("swarm.payload.output_id", output_id)

    def set_attribute(self, name: str, value: Any) -> None:
        self.attributes[name] = redact_value(value, name)
        self._span.set_attribute(name, _otel_attribute(value))

    def record_exception(self, exc: BaseException) -> None:
        if self._exception_recorded:
            return
        self._exception_recorded = True
        self._capture_input(is_error=True)
        output_id = self._capture_output(is_error=True)
        input_id = (
            self.hub._last_payload_id(self._span_id, "input")
            if self._input_captured
            else None
        )
        if input_id:
            self._span.set_attribute("swarm.payload.input_id", input_id)
        if output_id:
            self._span.set_attribute("swarm.payload.output_id", output_id)
        self._span.record_exception(exc)
        self._span.set_status(
            Status(StatusCode.ERROR, redact_text(str(exc))[:1024])
        )
        self.hub._record_exception(self.name, exc)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if exc is not None:
                self.record_exception(exc)
            elif self._exception_recorded:
                self._capture_input(is_error=True)
                self._capture_output(is_error=True)
            return bool(self._manager.__exit__(exc_type, exc, traceback))
        finally:
            self._unbind_context()


class _FallbackSpanHandle(_BaseSpanHandle):
    """SDK 不可用时的本地兼容路径；接口和持久化形状保持不变。"""

    def __enter__(self) -> "_FallbackSpanHandle":
        explicit = _normalize_link(self.context)
        self._trace_id = (
            explicit["trace_id"]
            if explicit
            else CURRENT_TRACE_ID.get() or uuid4().hex
        )
        self._parent_span_id = (
            explicit["span_id"] if explicit else CURRENT_SPAN_ID.get()
        )
        self._span_id = uuid4().hex[:16]
        self._started_at_epoch = time.time()
        self._status_code = "UNSET"
        self._status_message: str | None = None
        self._span_events: list[dict[str, Any]] = []
        self._bind_context()
        if self._run_id and not self._parent_span_id:
            self.hub._note_trace(self._run_id, self._trace_id)
        self._input_payload_id = self._capture_input()
        self._output_payload_id: str | None = None
        return self

    def set_output(self, payload: Any) -> None:
        super().set_output(payload)
        self._output_payload_id = self._capture_output()

    def set_attribute(self, name: str, value: Any) -> None:
        self.attributes[name] = redact_value(value, name)

    def record_exception(self, exc: BaseException) -> None:
        if self._exception_recorded:
            return
        self._exception_recorded = True
        self._input_payload_id = (
            self._input_payload_id or self._capture_input(is_error=True)
        )
        self._output_payload_id = (
            self._output_payload_id or self._capture_output(is_error=True)
        )
        self._status_code = "ERROR"
        self._status_message = redact_text(str(exc))[:1024]
        self._span_events.append(
            {
                "name": "exception",
                "timestamp": utc_now(),
                "attributes": {
                    "exception.type": type(exc).__name__,
                    "exception.message": redact_text(str(exc)),
                },
            }
        )
        self.hub._record_exception(self.name, exc)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if exc is not None:
                self.record_exception(exc)
            ended = time.time()
            self.hub.store.enqueue(
                "span",
                {
                    "span_id": self._span_id,
                    "trace_id": self._trace_id,
                    "parent_span_id": self._parent_span_id or None,
                    "run_id": self._run_id or None,
                    "task_id": _business_fields(self.attributes).get("task_id"),
                    "name": self.name,
                    "kind": self.kind.upper(),
                    "start_time": _iso_from_epoch(self._started_at_epoch),
                    "end_time": _iso_from_epoch(ended),
                    "duration_ms": max(
                        0.0, (ended - self._started_at_epoch) * 1000
                    ),
                    "status_code": self._status_code,
                    "status_message": self._status_message,
                    "attributes": {
                        **self.attributes,
                        "swarm.run.id": self._run_id or None,
                    },
                    "events": self._span_events,
                    "links": [
                        item
                        for item in (
                            _normalize_link(candidate)
                            for candidate in self.links
                        )
                        if item is not None
                    ],
                    "input_payload_id": self._input_payload_id,
                    "output_payload_id": self._output_payload_id,
                    "created_at": utc_now(),
                },
                priority=(
                    "critical"
                    if self._status_code == "ERROR"
                    or self.name in _CRITICAL_EVENTS
                    or self.name == "swarm_brain.chain1.run"
                    else "normal"
                ),
            )
            return False
        finally:
            self._unbind_context()


class ObservabilityHub:
    """V2 Trace/Log/Metric 的统一注入点。

    每个 Hub 都持有自己的 OTel Provider，不调用全局 ``set_tracer_provider``，
    因而测试和同进程多 runtime 不会互相覆盖 Provider。
    """

    def __init__(
        self,
        database_path: str | Path = DEFAULT_DB_PATH,
        *,
        capture_mode: str = "metadata",
        queue_capacity: int = 10_000,
        service_name: str = "swarm_brain.chain1",
        store: SQLiteObservationStore | None = None,
    ) -> None:
        self.payload_processor = PayloadProcessor(mode=capture_mode)
        self.capture_mode = capture_mode
        self.store = store or SQLiteObservationStore(
            database_path, queue_capacity=queue_capacity
        )
        self.service_name = service_name
        self.otel_enabled = OTEL_SDK_AVAILABLE
        self._run_lock = threading.RLock()
        self._active_runs: dict[str, dict[str, Any]] = {}
        self._run_modes: dict[str, str] = {}
        self._payload_index: dict[tuple[str, str], str] = {}
        self._metric_lock = threading.Lock()
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}
        self._logging_handlers: list[tuple[logging.Logger, SQLiteLogHandler]] = []
        self._closed = False
        if self.otel_enabled:
            try:
                resource = Resource.create(
                    {
                        "service.name": service_name,
                        "service.version": "0.1.0",
                    }
                )
                self._tracer_provider = TracerProvider(resource=resource)
                self._span_processor = BatchSpanProcessor(
                    SQLiteSpanExporter(self.store),
                    max_queue_size=min(10_000, max(2048, queue_capacity)),
                    max_export_batch_size=min(512, max(1, queue_capacity)),
                    schedule_delay_millis=200,
                )
                self._tracer_provider.add_span_processor(
                    self._span_processor
                )
                self._tracer = self._tracer_provider.get_tracer(
                    "swarm_brain.observability", "0.1.0"
                )
                self._meter_provider = MeterProvider(resource=resource)
                self._meter = self._meter_provider.get_meter(
                    "swarm_brain.observability", "0.1.0"
                )
                self._propagator = TraceContextTextMapPropagator()
            except Exception as exc:
                self.otel_enabled = False
                provider = getattr(self, "_tracer_provider", None)
                if provider is not None:
                    try:
                        provider.shutdown()
                    except Exception:
                        pass
                self.store.emergency.write(
                    {
                        "kind": "otel_initialization_error",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
        atexit.register(self.shutdown)

    # -- primary port -----------------------------------------------------

    def span(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
        links: list[Any] | None = None,
        input_payload: Any = None,
        *,
        context: Any = None,
        kind: str = "INTERNAL",
    ) -> _BaseSpanHandle:
        handle_type = _OtelSpanHandle if self.otel_enabled else _FallbackSpanHandle
        return handle_type(
            self,
            name,
            attributes,
            links,
            input_payload,
            context,
            kind,
        )

    def event(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
        payload: Any = None,
        level: str = "INFO",
    ) -> str:
        safe_attributes = _safe_attributes(attributes)
        context = {**correlation_context(), **_business_fields(safe_attributes)}
        # ``event_id`` in attributes is a Blackboard/business correlation key.
        # SQLite events need a distinct identifier because the same business
        # event can legitimately produce append/consume/status observations.
        observation_id = str(uuid4())
        level = level.upper()
        payload_id = self._capture_payload(
            payload,
            direction="event",
            is_error=level in {"ERROR", "CRITICAL"},
            run_id=str(context.get("run_id") or ""),
            trace_id=str(context.get("trace_id") or ""),
            span_id=str(context.get("span_id") or ""),
            event_id=observation_id,
        )
        error = safe_attributes.get("error")
        record = {
            **context,
            "event_id": observation_id,
            "name": name,
            "category": safe_attributes.pop("category", "observation"),
            "level": level,
            "timestamp": utc_now(),
            "attributes": safe_attributes,
            "payload_id": payload_id,
            "error_type": safe_attributes.get("error_type")
            or (type(error).__name__ if isinstance(error, BaseException) else None),
            "error_message": redact_text(str(error)) if error else None,
        }
        priority = (
            "critical"
            if level in {"ERROR", "CRITICAL"} or name in _CRITICAL_EVENTS
            else "debug"
            if level == "DEBUG"
            else "normal"
        )
        self.store.enqueue("event", record, priority=priority)
        if self.otel_enabled:
            current = otel_trace.get_current_span()
            if current.is_recording():
                current.add_event(name, _otel_attributes(safe_attributes))
        return observation_id

    def counter(
        self,
        name: str,
        value: int | float = 1,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        safe_attributes = self._metric_attributes(attributes)
        if self.otel_enabled:
            try:
                with self._metric_lock:
                    instrument = self._counters.get(name)
                    if instrument is None:
                        instrument = self._meter.create_counter(name)
                        self._counters[name] = instrument
                instrument.add(value, _otel_attributes(safe_attributes))
            except Exception:
                pass
        self._record_metric(name, "counter", value, safe_attributes)

    def histogram(
        self,
        name: str,
        value: int | float,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        safe_attributes = self._metric_attributes(attributes)
        if self.otel_enabled:
            try:
                with self._metric_lock:
                    instrument = self._histograms.get(name)
                    if instrument is None:
                        instrument = self._meter.create_histogram(name)
                        self._histograms[name] = instrument
                instrument.record(value, _otel_attributes(safe_attributes))
            except Exception:
                pass
        self._record_metric(name, "histogram", value, safe_attributes)

    def flush(self, timeout_s: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_s)
        otel_ok = True
        if self.otel_enabled:
            try:
                otel_ok = bool(
                    self._tracer_provider.force_flush(
                        timeout_millis=max(1, int(timeout_s * 1000))
                    )
                )
            except Exception:
                otel_ok = False
        remaining = max(0.0, deadline - time.monotonic())
        return self.store.flush(remaining) and otel_ok

    # -- run lifecycle ----------------------------------------------------

    def start_run(
        self,
        run_id: str | None = None,
        *,
        task_id: str | None = None,
        task_revision: int | None = None,
        coordination_epoch: int | None = None,
        capture_mode: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> str:
        run_id = run_id or str(uuid4())
        mode = capture_mode or self.capture_mode
        if mode not in {"metadata", "errors", "full"}:
            raise ValueError("capture_mode must be metadata, errors or full")
        started_epoch = time.time()
        with self._run_lock:
            self._active_runs[run_id] = {
                "started_epoch": started_epoch,
                "task_id": task_id,
                "attributes": _safe_attributes(attributes),
            }
            self._run_modes[run_id] = mode
        CURRENT_RUN_ID.set(run_id)
        fields = dict(CURRENT_FIELDS.get())
        fields.update(
            {
                key: value
                for key, value in {
                    "task_id": task_id,
                    "task_revision": task_revision,
                    "coordination_epoch": coordination_epoch,
                }.items()
                if value is not None
            }
        )
        CURRENT_FIELDS.set(fields)
        self.store.enqueue(
            "run_start",
            {
                "run_id": run_id,
                "task_id": task_id,
                "task_revision": task_revision,
                "coordination_epoch": coordination_epoch,
                "started_at": _iso_from_epoch(started_epoch),
                "status": "running",
                "capture_mode": mode,
                "attributes": attributes or {},
            },
            priority="critical",
        )
        self.counter("swarm_brain.run.total", attributes={"status": "started"})
        self.event(
            "run.started",
            {
                "category": "lifecycle",
                "task_id": task_id,
                "capture_mode": mode,
            },
        )
        return run_id

    def end_run(
        self,
        run_id: str,
        *,
        status: str = "ok",
        error: BaseException | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        ended_epoch = time.time()
        with self._run_lock:
            active = self._active_runs.pop(run_id, None) or {}
        if not CURRENT_TRACE_ID.get() and active.get("trace_id"):
            CURRENT_TRACE_ID.set(str(active["trace_id"]))
        duration_ms = max(
            0.0,
            (ended_epoch - active.get("started_epoch", ended_epoch)) * 1000,
        )
        error_type = type(error).__name__ if error else None
        error_message = redact_text(str(error)) if error else None
        merged_attributes = {
            **dict(active.get("attributes") or {}),
            **_safe_attributes(attributes),
        }
        level = "ERROR" if error or status not in {"ok", "success", "done"} else "INFO"
        self.event(
            "run.failed" if level == "ERROR" else "run.end",
            {
                "category": "lifecycle",
                "status": status,
                "duration_ms": duration_ms,
                "error_type": error_type,
                "error": error_message,
            },
            level=level,
        )
        self.store.enqueue(
            "run_end",
            {
                "run_id": run_id,
                "ended_at": _iso_from_epoch(ended_epoch),
                "duration_ms": duration_ms,
                "status": status,
                "error_type": error_type,
                "error_message": error_message,
                "attributes": merged_attributes,
            },
            priority="critical",
        )
        self.counter(
            "swarm_brain.run.completed",
            attributes={"status": status, "error_type": error_type or "none"},
        )
        self.histogram(
            "swarm_brain.run.duration",
            duration_ms,
            attributes={"status": status},
        )
        with self._run_lock:
            self._run_modes.pop(run_id, None)
        if CURRENT_RUN_ID.get() == run_id:
            CURRENT_RUN_ID.set("")
            CURRENT_TRACE_ID.set("")
            CURRENT_SPAN_ID.set("")
            CURRENT_PARENT_SPAN_ID.set("")
            CURRENT_FIELDS.set({})

    def update_run(
        self,
        run_id: str,
        *,
        task_id: str | None = None,
        task_revision: int | None = None,
        coordination_epoch: int | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        """在意图解析后补齐 run 的业务关联字段。"""

        merged_attributes: dict[str, Any] | None = None
        with self._run_lock:
            active = self._active_runs.get(run_id)
            if active is not None:
                if task_id is not None:
                    active["task_id"] = task_id
                if attributes is not None:
                    merged_attributes = {
                        **dict(active.get("attributes") or {}),
                        **_safe_attributes(attributes),
                    }
                    active["attributes"] = merged_attributes
            elif attributes is not None:
                merged_attributes = _safe_attributes(attributes)
        self.store.enqueue(
            "run_update",
            {
                "run_id": run_id,
                "task_id": task_id,
                "task_revision": task_revision,
                "coordination_epoch": coordination_epoch,
                "attributes": merged_attributes,
            },
            priority="critical",
        )
        if CURRENT_RUN_ID.get() == run_id:
            fields = dict(CURRENT_FIELDS.get())
            fields.update(
                {
                    key: value
                    for key, value in {
                        "task_id": task_id,
                        "task_revision": task_revision,
                        "coordination_epoch": coordination_epoch,
                    }.items()
                    if value is not None
                }
            )
            CURRENT_FIELDS.set(fields)

    # -- propagation / span links ----------------------------------------

    def inject_carrier(
        self, carrier: dict[str, str] | None = None
    ) -> dict[str, str]:
        target = carrier if carrier is not None else {}
        if self.otel_enabled:
            self._propagator.inject(target)
            return target
        trace_id = CURRENT_TRACE_ID.get()
        span_id = CURRENT_SPAN_ID.get()
        if trace_id and span_id:
            target["traceparent"] = f"00-{trace_id}-{span_id}-01"
        return target

    def current_carrier(self) -> dict[str, str]:
        return self.inject_carrier({})

    def extract_carrier(self, carrier: Mapping[str, str] | None) -> Any:
        if not carrier:
            return None
        if self.otel_enabled:
            return self._propagator.extract(dict(carrier))
        match = _TRACEPARENT.match(str(carrier.get("traceparent", "")))
        if not match:
            return None
        return {
            "trace_id": match.group(1).lower(),
            "span_id": match.group(2).lower(),
            "trace_flags": match.group(3).lower(),
            "traceparent": match.group(0).lower(),
        }

    @contextmanager
    def consumer_context(self, carrier: Mapping[str, str] | None):
        """把事件携带的 W3C 上下文临时设为当前消费上下文。

        无 carrier 或格式无效时退化为普通本地父上下文，不抛异常。
        """

        extracted = self.extract_carrier(carrier)
        if extracted is None:
            yield
            return
        if self.otel_enabled:
            token = None
            try:
                token = otel_attach(extracted)
                yield
            finally:
                if token is not None:
                    otel_detach(token)
            return
        normalized = _normalize_link(extracted)
        if normalized is None:
            yield
            return
        trace_token = CURRENT_TRACE_ID.set(normalized["trace_id"])
        span_token = CURRENT_SPAN_ID.set(normalized["span_id"])
        parent_token = CURRENT_PARENT_SPAN_ID.set("")
        try:
            yield
        finally:
            CURRENT_PARENT_SPAN_ID.reset(parent_token)
            CURRENT_SPAN_ID.reset(span_token)
            CURRENT_TRACE_ID.reset(trace_token)

    def _to_otel_link(self, value: Any) -> Any:
        if not self.otel_enabled:
            return None
        if isinstance(value, Link):
            return value
        if isinstance(value, SpanContext):
            return Link(value)
        normalized = _normalize_link(value)
        if not normalized:
            return None
        context = SpanContext(
            trace_id=int(normalized["trace_id"], 16),
            span_id=int(normalized["span_id"], 16),
            is_remote=True,
            trace_flags=TraceFlags(
                int(normalized.get("trace_flags", "01"), 16)
            ),
            trace_state=TraceState(),
        )
        return Link(context)

    # -- logging ----------------------------------------------------------

    def configure_logging(
        self,
        logger: logging.Logger | None = None,
        *,
        level: int = logging.INFO,
    ) -> SQLiteLogHandler:
        target = logger or logging.getLogger()
        for handler in target.handlers:
            if (
                isinstance(handler, SQLiteLogHandler)
                and getattr(handler, "_swarm_observer_id", None) == id(self)
            ):
                return handler
        handler = SQLiteLogHandler(self, level=level)
        target.addHandler(handler)
        if target.level == logging.NOTSET or target.level > level:
            target.setLevel(level)
        self._logging_handlers.append((target, handler))
        return handler

    def _record_log(self, record: dict[str, Any]) -> None:
        priority = (
            "critical"
            if record.get("level") in {"ERROR", "CRITICAL"}
            else "debug"
            if record.get("level") == "DEBUG"
            else "normal"
        )
        self.store.enqueue("log", record, priority=priority)

    def _record_exception(self, operation: str, exc: BaseException) -> None:
        stack = redact_text(
            "".join(
                traceback_module.format_exception(
                    type(exc), exc, exc.__traceback__
                )
            )
        )
        context = correlation_context()
        self.store.enqueue(
            "log",
            {
                "log_id": str(uuid4()),
                **context,
                "timestamp": utc_now(),
                "level": "ERROR",
                "module": "swarm_brain.observability",
                "operation": operation,
                "message": redact_text(str(exc)),
                "error_type": type(exc).__name__,
                "stack_trace": stack,
                "attributes": {},
            },
            priority="critical",
        )
        self.event(
            "exception",
            {
                "category": "exception",
                "operation": operation,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            level="ERROR",
        )

    # -- payload / metrics / assets --------------------------------------

    def _capture_payload(
        self,
        payload: Any,
        *,
        direction: str,
        is_error: bool,
        run_id: str,
        trace_id: str,
        span_id: str,
        event_id: str | None = None,
    ) -> str | None:
        if payload is None:
            return None
        mode = self._run_modes.get(run_id, self.capture_mode)
        try:
            prepared = self.payload_processor.prepare(
                payload,
                direction=direction,
                is_error=is_error,
                capture_mode=mode,
            )
        except Exception as exc:
            self.store.emergency.write(
                {
                    "kind": "payload_capture_error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "direction": direction,
                }
            )
            return None
        if prepared is None:
            return None
        prepared.update(
            {
                "run_id": run_id or None,
                "trace_id": trace_id or None,
                "span_id": span_id or None,
                "event_id": event_id,
                "created_at": utc_now(),
            }
        )
        payload_id = str(prepared["payload_id"])
        self._payload_index[(span_id, direction)] = payload_id
        self.store.enqueue("payload", prepared, priority="debug")
        return payload_id

    def _last_payload_id(self, span_id: str, direction: str) -> str | None:
        return self._payload_index.get((span_id, direction))

    def _record_metric(
        self,
        name: str,
        instrument_type: str,
        value: int | float,
        attributes: Mapping[str, Any],
    ) -> None:
        self.store.enqueue(
            "metric",
            {
                "run_id": CURRENT_RUN_ID.get() or None,
                "timestamp": utc_now(),
                "metric_name": name,
                "instrument_type": instrument_type,
                "value": float(value),
                "attributes": attributes,
            },
            priority="debug",
        )

    @staticmethod
    def _metric_attributes(
        attributes: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        safe = _safe_attributes(attributes)
        return {
            key: value
            for key, value in safe.items()
            if not ObservabilityHub._is_high_cardinality_metric_key(key)
        }

    @staticmethod
    def _is_high_cardinality_metric_key(key: str) -> bool:
        normalized = key.lower().replace(".", "_").replace("-", "_")
        if normalized in _HIGH_CARDINALITY_METRIC_KEYS:
            return True
        return any(
            normalized.endswith("_" + item)
            for item in _HIGH_CARDINALITY_METRIC_KEYS
        )

    def save_trace_asset(self, asset: Mapping[str, Any]) -> bool:
        episode = _safe_attributes(asset)
        record = dict(episode)
        record.setdefault("asset_id", str(uuid4()))
        record.setdefault("run_id", CURRENT_RUN_ID.get() or None)
        record.setdefault("trace_id", CURRENT_TRACE_ID.get() or None)
        record["quality_status"] = str(
            record.get("quality_status")
            or record.get("status")
            or "raw"
        )
        record["episode"] = episode
        record.setdefault(
            "source_span_ids", list(episode.get("span_ids") or [])
        )
        if "source_event_ids" not in record:
            record["source_event_ids"] = [
                str(item.get("event_id"))
                for item in episode.get("event_refs", [])
                if isinstance(item, Mapping) and item.get("event_id")
            ]
        record["quality"] = {
            "redaction_clean": bool(episode.get("redaction_clean", False)),
            "terminal_status": episode.get("terminal_status"),
        }
        created_at = record.get("created_at")
        if isinstance(created_at, (int, float)):
            record["created_at"] = _iso_from_epoch(float(created_at))
        elif not created_at:
            record["created_at"] = utc_now()
        record["updated_at"] = utc_now()
        record.setdefault(
            "expires_at",
            (
                datetime.now(timezone.utc) + timedelta(days=180)
            ).isoformat(timespec="milliseconds"),
        )
        return self.store.enqueue("trace_asset", record, priority="normal")

    # -- query facade -----------------------------------------------------

    def list_runs(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.store.list_runs(**kwargs)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.store.get_run(run_id)

    def query_timeline(self, run_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self.store.query_timeline(run_id, **kwargs)

    def query_trace(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.query_trace(run_id)

    def query_logs(self, run_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self.store.query_logs(run_id, **kwargs)

    def query_errors(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.query_errors(run_id)

    def query_metrics(
        self, run_id: str | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return self.store.query_metrics(run_id, **kwargs)

    def get_payload(self, payload_id: str) -> dict[str, Any] | None:
        return self.store.get_payload(payload_id)

    def query_trace_assets(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.store.query_trace_assets(**kwargs)

    def get_events_since(self, sequence: int, **kwargs: Any) -> list[dict[str, Any]]:
        return self.store.get_events_since(sequence, **kwargs)

    def health(self) -> dict[str, Any]:
        health = self.store.health()
        health["otel_enabled"] = self.otel_enabled
        health["capture_mode"] = self.capture_mode
        return health

    # -- shutdown ---------------------------------------------------------

    def shutdown(self, timeout_s: float = 5.0) -> bool:
        if self._closed:
            return True
        self._closed = True
        for logger, handler in self._logging_handlers:
            try:
                logger.removeHandler(handler)
                handler.close()
            except (ValueError, OSError):
                pass
        if self.otel_enabled:
            try:
                self._tracer_provider.force_flush(
                    timeout_millis=max(1, int(timeout_s * 1000))
                )
                self._tracer_provider.shutdown()
                self._meter_provider.shutdown()
            except Exception:
                pass
        return self.store.close(timeout_s)

    def _note_trace(self, run_id: str, trace_id: str) -> None:
        with self._run_lock:
            active = self._active_runs.get(run_id)
            if active is not None:
                active["trace_id"] = trace_id
        self.store.enqueue(
            "run_trace",
            {"run_id": run_id, "trace_id": trace_id},
            priority="critical",
        )
