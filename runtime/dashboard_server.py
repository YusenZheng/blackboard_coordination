"""Local observability dashboard for the V2 coordination runtime.

The HTTP layer intentionally uses only the Python standard library.  Live
callbacks remain available for low-latency rendering while optional
``observability_store`` and ``memory_store`` objects provide historical
queries.  Those stores are duck-typed so the dashboard remains usable when
observability is disabled or degraded.
"""
from __future__ import annotations

import argparse
import dataclasses
import inspect
import ipaddress
import json
import logging
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, urlparse

from .coordination_runtime import CoordinationRuntime


MAX_REQUEST_BYTES = 64 * 1024
MAX_QUERY_LIMIT = 10_000
DEFAULT_QUERY_LIMIT = 500
LOGGER = logging.getLogger(__name__)


class StoreUnavailable(RuntimeError):
    """Raised internally when an optional dashboard data source is absent."""


@dataclass
class LiveRun:
    run_id: str
    instruction: str
    capture_mode: str = "metadata"
    messages: list[dict] = field(default_factory=list)
    done: bool = False
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    _event_sink: Optional[Callable[[dict], None]] = field(
        default=None, repr=False
    )
    _condition: threading.Condition = field(
        default_factory=threading.Condition, repr=False
    )

    def publish(self, kind: str, data: dict) -> None:
        with self._condition:
            message = {
                "seq": len(self.messages) + 1,
                "kind": kind,
                "at": time.time(),
                "data": data,
            }
            self.messages.append(message)
            self._condition.notify_all()
        if self._event_sink is not None:
            try:
                self._event_sink(message)
            except Exception:
                # Dashboard persistence must never change coordination outcome.
                LOGGER.exception("dashboard stream event persistence failed")

    def finish(
        self, *, result: Optional[dict] = None, error: Optional[str] = None
    ) -> None:
        with self._condition:
            self.result = result
            self.error = error
            self.done = True
            self.finished_at = time.time()
            self._condition.notify_all()

    def wait_after(self, seq: int, timeout_s: float) -> tuple[list[dict], bool]:
        with self._condition:
            if len(self.messages) <= seq and not self.done:
                self._condition.wait(timeout_s)
            return list(self.messages[seq:]), self.done

    def snapshot(self) -> dict:
        with self._condition:
            return {
                "run_id": self.run_id,
                "instruction": self.instruction,
                "capture_mode": self.capture_mode,
                "message_count": len(self.messages),
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "done": self.done,
                "status": (
                    "failed" if self.error else "completed" if self.done else "running"
                ),
                "result": self.result,
                "error": self.error,
            }


class RunRegistry:
    def __init__(
        self,
        runtime_factory: Callable[[], CoordinationRuntime] = CoordinationRuntime,
        *,
        observability_store: Any = None,
        memory_store: Any = None,
        owns_stores: bool = False,
    ) -> None:
        self.runtime_factory = runtime_factory
        self.observability_store = observability_store
        self.memory_store = memory_store
        self.owns_stores = owns_stores
        self._runs: dict[str, LiveRun] = {}
        self._lock = threading.Lock()

    def start(
        self, instruction: str, *, capture_mode: str = "metadata"
    ) -> LiveRun:
        if capture_mode not in {"metadata", "errors", "full"}:
            raise ValueError("invalid capture mode")
        with self._lock:
            if any(not item.done for item in self._runs.values()):
                raise RuntimeError("已有任务正在运行，请等待当前任务结束")
            run_id = uuid.uuid4().hex
            state = LiveRun(
                run_id=run_id,
                instruction=instruction,
                capture_mode=capture_mode,
                _event_sink=lambda message: self._persist_stream_event(
                    run_id, message
                ),
            )
            self._runs[run_id] = state
        self._best_effort_store_call(
            self.observability_store,
            ("dashboard_run_started", "record_run_started"),
            [
                (
                    (
                        run_id,
                        instruction,
                        state.created_at,
                        state.capture_mode,
                    ),
                    {},
                ),
                ((run_id, instruction, state.created_at), {}),
                ((run_id, instruction), {}),
                (
                    (),
                    {
                        "run_id": run_id,
                        "instruction": instruction,
                        "started_at": state.created_at,
                        "capture_mode": state.capture_mode,
                    },
                ),
            ],
        )
        thread = threading.Thread(
            target=self._execute,
            args=(state,),
            name=f"coordination-dashboard-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return state

    def get(self, run_id: str) -> Optional[LiveRun]:
        with self._lock:
            return self._runs.get(run_id)

    def list_runs(self, *, limit: int = 100, offset: int = 0) -> dict:
        persisted = self._query_store(
            self.observability_store,
            ("list_runs", "query_runs"),
            [
                ((), {"limit": limit + offset, "offset": 0}),
                ((limit + offset, 0), {}),
                ((), {}),
            ],
            default=[],
        )
        persisted_items = _items_from_result(persisted, ("runs", "items"))
        merged: dict[str, dict] = {}
        for item in persisted_items:
            row = _as_dict(item)
            run_id = str(row.get("run_id") or row.get("id") or "")
            if run_id:
                merged[run_id] = row
        with self._lock:
            live = [item.snapshot() for item in self._runs.values()]
        for row in live:
            previous = merged.get(row["run_id"], {})
            merged[row["run_id"]] = {**previous, **row}
        rows = sorted(
            merged.values(),
            key=lambda row: _timestamp_sort_value(
                row.get("created_at")
                or row.get("started_at")
                or row.get("start_time")
                or 0
            ),
            reverse=True,
        )
        return {
            "runs": rows[offset : offset + limit],
            "count": len(rows),
            "live_count": sum(not row["done"] for row in live),
            "degraded": self.observability_store is None,
        }

    def run_detail(self, run_id: str) -> Optional[dict]:
        persisted = self._query_store(
            self.observability_store,
            ("get_run", "run_detail", "query_run"),
            [((run_id,), {}), ((), {"run_id": run_id})],
            default=None,
        )
        row = _as_dict(persisted) if persisted is not None else {}
        live = self.get(run_id)
        if live is not None:
            row = {**row, **live.snapshot()}
        return row or None

    def timeline(self, run_id: str, *, since: int = 0, limit: int = DEFAULT_QUERY_LIMIT) -> dict:
        result = self._query_store(
            self.observability_store,
            ("get_timeline", "timeline", "query_timeline", "list_events"),
            [
                ((run_id,), {"since": since, "limit": limit}),
                ((run_id, since, limit), {}),
                ((run_id,), {}),
                ((), {"run_id": run_id, "since": since, "limit": limit}),
            ],
            default=None,
        )
        if result is None:
            state = self.get(run_id)
            items = [] if state is None else state.wait_after(since, 0)[0][:limit]
        else:
            items = _items_from_result(result, ("events", "timeline", "items"))
        rows: list[dict] = []
        for item in items:
            row = _as_dict(item)
            category = str(row.get("category") or "")
            name = str(row.get("name") or "")
            if category == "dashboard_stream":
                # Transport replay records are exposed by the SSE endpoint;
                # the historical timeline uses the canonical observations.
                continue
            if row.get("payload_id") and (
                category == "blackboard"
                or name == "dashboard.stream.blackboard_event"
            ):
                payload = self.payload(run_id, str(row["payload_id"]))
                if payload:
                    raw_content = payload.get("redacted_content")
                    if raw_content is None:
                        raw_content = payload.get("summary")
                    row["payload"] = _parse_json_value(raw_content)
                    row["payload_summary"] = payload.get("summary")
            rows.append(row)
        return {
            "run_id": run_id,
            "events": rows,
            "since": since,
            "degraded": self.observability_store is None,
        }

    def trace(self, run_id: str) -> dict:
        result = self._query_store(
            self.observability_store,
            ("get_trace", "trace_tree", "query_trace", "list_spans"),
            [
                ((run_id,), {}),
                ((), {"run_id": run_id}),
            ],
            default=[],
        )
        if isinstance(result, dict):
            value = _as_dict(result)
            value.setdefault("run_id", run_id)
            value.setdefault("spans", [])
            value.setdefault("links", [])
            return value
        return {
            "run_id": run_id,
            "spans": [_as_dict(item) for item in _items_from_result(result)],
            "links": [],
            "degraded": self.observability_store is None,
        }

    def logs(
        self,
        run_id: str,
        *,
        level: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = DEFAULT_QUERY_LIMIT,
    ) -> dict:
        result = self._query_store(
            self.observability_store,
            ("get_logs", "query_logs", "list_logs"),
            [
                (
                    (run_id,),
                    {"level": level, "search": search, "limit": limit},
                ),
                ((run_id,), {"level": level, "limit": limit}),
                ((run_id,), {"limit": limit}),
                ((run_id,), {}),
                (
                    (),
                    {
                        "run_id": run_id,
                        "level": level,
                        "search": search,
                        "limit": limit,
                    },
                ),
            ],
            default=[],
        )
        rows = [_as_dict(item) for item in _items_from_result(result, ("logs", "items"))]
        if search:
            needle = search.casefold()
            rows = [
                row
                for row in rows
                if needle
                in json.dumps(row, ensure_ascii=False, default=_json_default).casefold()
            ]
        return {
            "run_id": run_id,
            "logs": rows,
            "degraded": self.observability_store is None,
        }

    def errors(self, run_id: str, *, limit: int = DEFAULT_QUERY_LIMIT) -> dict:
        result = self._query_store(
            self.observability_store,
            ("get_errors", "query_errors", "list_errors"),
            [
                ((run_id,), {"limit": limit}),
                ((run_id,), {}),
                ((), {"run_id": run_id, "limit": limit}),
            ],
            default=None,
        )
        if result is None:
            rows = [
                row
                for row in self.logs(run_id, limit=limit)["logs"]
                if str(row.get("level", "")).upper() in {"ERROR", "CRITICAL"}
                or row.get("error_type")
                or row.get("exception")
            ]
        else:
            rows = [_as_dict(item) for item in _items_from_result(result, ("errors", "items"))]
        live = self.get(run_id)
        if live is not None and live.error:
            rows.append({"level": "ERROR", "message": live.error, "source": "dashboard"})
        return {
            "run_id": run_id,
            "errors": rows,
            "degraded": self.observability_store is None,
        }

    def metrics(self, run_id: str) -> dict:
        result = self._query_store(
            self.observability_store,
            ("get_metrics", "query_metrics", "list_metric_points"),
            [((run_id,), {}), ((), {"run_id": run_id})],
            default=[],
        )
        if isinstance(result, dict):
            value = _as_dict(result)
            value.setdefault("run_id", run_id)
            value.setdefault("metrics", value.pop("items", []))
            return value
        return {
            "run_id": run_id,
            "metrics": [_as_dict(item) for item in _items_from_result(result)],
            "degraded": self.observability_store is None,
        }

    def payload(self, run_id: str, payload_id: str) -> Optional[dict]:
        result = self._query_store(
            self.observability_store,
            ("get_payload", "query_payload"),
            [
                ((run_id, payload_id), {}),
                ((payload_id,), {}),
                ((), {"run_id": run_id, "payload_id": payload_id}),
                ((), {"payload_id": payload_id}),
            ],
            default=None,
        )
        if result is None:
            return None
        row = _as_dict(result)
        owner = row.get("run_id")
        if owner not in (None, "", run_id):
            return None
        return row

    def memory_candidates(self, *, status: str = "pending_review", limit: int = 200) -> dict:
        if status == "all":
            combined: list[dict] = []
            for candidate_status in (
                "pending_review",
                "approved",
                "rejected",
                "archived",
            ):
                combined.extend(
                    self.memory_candidates(
                        status=candidate_status, limit=limit
                    )["candidates"]
                )
            combined.sort(
                key=lambda row: str(
                    row.get("created_at")
                    or row.get("updated_at")
                    or 0
                ),
                reverse=True,
            )
            return {
                "candidates": combined[:limit],
                "status": status,
                "degraded": self.memory_store is None,
            }
        result = self._query_store(
            self.memory_store,
            ("list_candidates", "get_candidates", "query_candidates"),
            [
                ((), {"status": status, "limit": limit}),
                ((status, limit), {}),
                ((), {"status": status}),
                ((), {}),
            ],
            default=[],
        )
        return {
            "candidates": [
                _as_dict(item)
                for item in _items_from_result(result, ("candidates", "items"))
            ],
            "status": status,
            "degraded": self.memory_store is None,
        }

    def decide_memory_candidate(
        self,
        candidate_id: str,
        *,
        decision: str,
        notes: Optional[str] = None,
    ) -> Optional[dict]:
        result = self._required_store_call(
            self.memory_store,
            (
                "decide_candidate",
                "review_candidate",
                "set_candidate_decision",
            ),
            [
                (
                    (candidate_id, decision),
                    {
                        "decided_by": "dashboard",
                        "reason": notes or "",
                    },
                ),
                (
                    (candidate_id, decision),
                    {"notes": notes, "reviewer": "local-dashboard"},
                ),
                ((candidate_id, decision), {"notes": notes}),
                ((candidate_id, decision), {}),
                (
                    (),
                    {
                        "candidate_id": candidate_id,
                        "decision": decision,
                        "notes": notes,
                        "reviewer": "local-dashboard",
                    },
                ),
            ],
        )
        return _as_dict(result) if result is not None else {
            "id": candidate_id,
            "status": decision,
        }

    def stream_events_since(self, run_id: str, since: int) -> list[dict]:
        if self.has_persistent_event_stream:
            persisted = self._query_store(
                self.observability_store,
                ("get_events_since",),
                [
                    ((since,), {"run_id": run_id, "limit": 10_000}),
                    ((since,), {"run_id": run_id}),
                    ((since,), {}),
                ],
                default=[],
            )
        else:
            persisted = self._query_store(
                self.observability_store,
                (
                    "get_stream_events",
                    "stream_events_since",
                    "list_dashboard_events",
                ),
                [
                    ((run_id,), {"since": since}),
                    ((run_id, since), {}),
                    ((), {"run_id": run_id, "since": since}),
                ],
                default=[],
            )
        rows = []
        raw_items = _items_from_result(persisted, ("events", "items"))
        for item in raw_items:
            record = _as_dict(item)
            if (
                self.has_persistent_event_stream
                and record.get("category") != "dashboard_stream"
            ):
                continue
            payload_id = record.get("payload_id")
            if payload_id and not record.get("payload"):
                payload = self.payload(run_id, str(payload_id))
                if payload:
                    raw_content = payload.get("redacted_content")
                    if raw_content is None:
                        raw_content = payload.get("summary")
                    record["payload"] = _parse_json_value(raw_content)
            rows.append(_normalise_stream_message(record))
        live = self.get(run_id)
        if live is not None:
            rows.extend(
                _normalise_stream_message(item)
                for item in live.wait_after(0, 0)[0]
            )
        deduped: dict[int, dict] = {}
        for row in rows:
            seq = _message_sequence(row)
            if seq > since:
                deduped[seq] = row
        return [deduped[key] for key in sorted(deduped)]

    @property
    def has_persistent_event_stream(self) -> bool:
        return callable(
            getattr(self.observability_store, "get_events_since", None)
        )

    def health(self) -> dict:
        details: dict[str, Any] = {}
        degraded = False
        for name, store in (
            ("observability", self.observability_store),
            ("memory", self.memory_store),
        ):
            if store is None:
                details[name] = {"status": "disabled"}
                continue
            result = self._query_store(
                store,
                ("health", "get_health", "status"),
                [((), {})],
                default={"status": "ok"},
            )
            details[name] = _as_dict(result)
            if "status" not in details[name] and "ok" in details[name]:
                details[name]["status"] = (
                    "ok" if details[name]["ok"] else "degraded"
                )
            if str(details[name].get("status", "ok")).lower() not in {
                "ok",
                "healthy",
            }:
                degraded = True
        return {
            "status": "degraded" if degraded else "ok",
            "service": "coordination-dashboard",
            "stores": details,
        }

    def flush_observability(self, timeout_s: float = 0.5) -> None:
        self._best_effort_store_call(
            self.observability_store,
            ("flush",),
            [
                ((timeout_s,), {}),
                ((), {"timeout_s": timeout_s}),
                ((), {}),
            ],
        )

    def close(self) -> None:
        if not self.owns_stores:
            return
        self.flush_observability(2.0)
        self._best_effort_store_call(
            self.observability_store,
            ("shutdown", "close"),
            [((5.0,), {}), ((), {"timeout_s": 5.0}), ((), {})],
        )
        self._best_effort_store_call(
            self.memory_store, ("close",), [((), {})]
        )

    def _execute(self, state: LiveRun) -> None:
        try:
            runtime = self.runtime_factory()
            listeners = {
                "event_listener": lambda data: state.publish(
                    "blackboard_event", data
                ),
                "status_listener": lambda data: state.publish("status", data),
                "llm_listener": lambda data: state.publish("llm_call", data),
                "session_listener": lambda data: state.publish(
                    "agent_session", data
                ),
                "tool_listener": lambda data: state.publish("tool_call", data),
            }
            parameters = inspect.signature(runtime.run).parameters
            kwargs = {
                name: callback
                for name, callback in listeners.items()
                if name in parameters
            }
            if "run_id" in parameters:
                kwargs["run_id"] = state.run_id
            if "capture_mode" in parameters:
                kwargs["capture_mode"] = state.capture_mode
            result = runtime.run(state.instruction, **kwargs)
            state.publish("result", result)
            state.finish(result=result)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            state.publish("error", {"message": error})
            state.finish(error=error)
        finally:
            self._best_effort_store_call(
                self.observability_store,
                ("dashboard_run_finished", "record_run_finished"),
                [
                    (
                        (state.run_id,),
                        {
                            "result": state.result,
                            "error": state.error,
                            "finished_at": state.finished_at,
                        },
                    ),
                    (
                        (),
                        {
                            "run_id": state.run_id,
                            "result": state.result,
                            "error": state.error,
                            "finished_at": state.finished_at,
                        },
                    ),
                ],
            )

    def _persist_stream_event(self, run_id: str, message: dict) -> None:
        store = self.observability_store
        dedicated_names = (
            "append_stream_event",
            "record_stream_event",
            "append_dashboard_event",
        )
        if any(callable(getattr(store, name, None)) for name in dedicated_names):
            self._best_effort_store_call(
                store,
                dedicated_names,
                [
                    ((run_id, message), {}),
                    (
                        (
                            run_id,
                            message["seq"],
                            message["kind"],
                            message["data"],
                        ),
                        {"at": message["at"]},
                    ),
                    ((), {"run_id": run_id, "message": message}),
                    (
                        (),
                        {
                            "run_id": run_id,
                            "seq": message["seq"],
                            "kind": message["kind"],
                            "data": message["data"],
                            "at": message["at"],
                        },
                    ),
                ],
            )
            return
        event_method = getattr(store, "event", None)
        if callable(event_method):
            try:
                event_method(
                    f"dashboard.stream.{message['kind']}",
                    {
                        "category": "dashboard_stream",
                        "run_id": run_id,
                        "dashboard_seq": message["seq"],
                        "dashboard_at": message["at"],
                    },
                    message["data"],
                    level=(
                        "ERROR" if message["kind"] == "error" else "INFO"
                    ),
                )
            except Exception:
                LOGGER.exception("dashboard stream event persistence failed")

    @staticmethod
    def _query_store(
        store: Any,
        names: Iterable[str],
        variants: list[tuple[tuple, dict]],
        *,
        default: Any,
    ) -> Any:
        try:
            return RunRegistry._required_store_call(store, names, variants)
        except StoreUnavailable:
            return default
        except Exception:
            LOGGER.exception("dashboard store query failed")
            return default

    @staticmethod
    def _best_effort_store_call(
        store: Any,
        names: Iterable[str],
        variants: list[tuple[tuple, dict]],
    ) -> None:
        try:
            RunRegistry._required_store_call(store, names, variants)
        except StoreUnavailable:
            return
        except Exception:
            LOGGER.exception("dashboard store write failed")

    @staticmethod
    def _required_store_call(
        store: Any,
        names: Iterable[str],
        variants: list[tuple[tuple, dict]],
    ) -> Any:
        if store is None:
            raise StoreUnavailable("store is disabled")
        for name in names:
            method = getattr(store, name, None)
            if not callable(method):
                continue
            signature = None
            try:
                signature = inspect.signature(method)
            except (TypeError, ValueError):
                pass
            for args, kwargs in variants:
                clean_kwargs = {
                    key: value for key, value in kwargs.items() if value is not None
                }
                if signature is not None:
                    try:
                        signature.bind(*args, **clean_kwargs)
                    except TypeError:
                        continue
                return method(*args, **clean_kwargs)
        raise StoreUnavailable(
            f"store does not implement any of: {', '.join(names)}"
        )


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        registry: Optional[RunRegistry] = None,
        *,
        observability_store: Any = None,
        memory_store: Any = None,
    ) -> None:
        _require_loopback(server_address[0])
        super().__init__(server_address, DashboardHandler)
        self.registry = registry or RunRegistry(
            observability_store=observability_store,
            memory_store=memory_store,
        )
        self.dashboard_html = (
            Path(__file__).with_name("dashboard.html").read_bytes()
        )


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: DashboardHTTPServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/dashboard.html"):
            self._send_bytes(
                HTTPStatus.OK,
                self.server.dashboard_html,
                "text/html; charset=utf-8",
                cache=False,
            )
            return
        if parsed.path == "/favicon.ico":
            self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return
        if parsed.path == "/api/health":
            self._send_json(HTTPStatus.OK, self.server.registry.health())
            return
        if parsed.path == "/api/runs":
            query = parse_qs(parsed.query)
            limit = self._query_int(query, "limit", 100, minimum=1)
            offset = self._query_int(query, "offset", 0, minimum=0)
            if limit is None or offset is None:
                return
            self._send_json(
                HTTPStatus.OK,
                self.server.registry.list_runs(limit=limit, offset=offset),
            )
            return
        if parsed.path == "/api/memory/candidates":
            query = parse_qs(parsed.query)
            limit = self._query_int(query, "limit", 200, minimum=1)
            if limit is None:
                return
            status = query.get("status", ["pending_review"])[0]
            if status not in {
                "pending_review",
                "approved",
                "rejected",
                "archived",
                "all",
            }:
                self._send_json(
                    HTTPStatus.BAD_REQUEST, {"error": "invalid_status"}
                )
                return
            self._send_json(
                HTTPStatus.OK,
                self.server.registry.memory_candidates(
                    status=status, limit=limit
                ),
            )
            return

        run_id, suffix = _parse_run_path(parsed.path)
        if run_id is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        state = self.server.registry.get(run_id)
        detail = self.server.registry.run_detail(run_id)
        if state is None and detail is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
            return
        if suffix == "events":
            try:
                query_since = int(
                    parse_qs(parsed.query).get("since", ["0"])[0]
                )
                header_since = int(self.headers.get("Last-Event-ID", "0"))
                since = max(0, query_since, header_since)
            except (TypeError, ValueError):
                self._send_json(
                    HTTPStatus.BAD_REQUEST, {"error": "invalid_since"}
                )
                return
            self._stream_events(run_id, state, since)
            return
        if suffix is None:
            self._send_json(HTTPStatus.OK, detail)
            return
        query = parse_qs(parsed.query)
        limit = self._query_int(
            query, "limit", DEFAULT_QUERY_LIMIT, minimum=1
        )
        if limit is None:
            return
        if suffix == "timeline":
            since = self._query_int(query, "since", 0, minimum=0)
            if since is None:
                return
            self._send_json(
                HTTPStatus.OK,
                self.server.registry.timeline(
                    run_id, since=since, limit=limit
                ),
            )
            return
        if suffix == "trace":
            self._send_json(
                HTTPStatus.OK, self.server.registry.trace(run_id)
            )
            return
        if suffix == "logs":
            self._send_json(
                HTTPStatus.OK,
                self.server.registry.logs(
                    run_id,
                    level=query.get("level", [None])[0],
                    search=query.get("q", [None])[0],
                    limit=limit,
                ),
            )
            return
        if suffix == "errors":
            self._send_json(
                HTTPStatus.OK,
                self.server.registry.errors(run_id, limit=limit),
            )
            return
        if suffix == "metrics":
            self._send_json(
                HTTPStatus.OK, self.server.registry.metrics(run_id)
            )
            return
        if suffix.startswith("payloads/"):
            payload_id = suffix.split("/", 1)[1]
            payload = self.server.registry.payload(run_id, payload_id)
            if payload is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND, {"error": "payload_not_found"}
                )
                return
            self._send_json(HTTPStatus.OK, payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        parsed = urlparse(self.path)
        candidate_id = _parse_candidate_decision_path(parsed.path)
        if parsed.path != "/api/runs" and candidate_id is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        body = self._read_json_body()
        if body is None:
            return
        if candidate_id is not None:
            self._handle_candidate_decision(candidate_id, body)
            return
        self._handle_start_run(body)

    def _handle_start_run(self, body: Any) -> None:
        instruction = body.get("instruction") if isinstance(body, dict) else None
        if not isinstance(instruction, str) or not instruction.strip():
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "instruction_required"}
            )
            return
        if len(instruction) > 1000:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "instruction_too_long"}
            )
            return
        capture_mode = body.get("capture_mode", "metadata")
        if capture_mode not in {"metadata", "errors", "full"}:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "invalid_capture_mode"}
            )
            return
        try:
            state = self.server.registry.start(
                instruction.strip(), capture_mode=capture_mode
            )
        except RuntimeError as exc:
            self._send_json(
                HTTPStatus.CONFLICT,
                {"error": "run_in_progress", "message": str(exc)},
            )
            return
        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "run_id": state.run_id,
                "events_url": f"/api/runs/{state.run_id}/events",
            },
        )

    def _handle_candidate_decision(
        self, candidate_id: str, body: Any
    ) -> None:
        if not isinstance(body, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_body"})
            return
        decision = body.get("decision")
        if decision not in {"approved", "rejected"}:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "invalid_decision"}
            )
            return
        notes = body.get("notes")
        if notes is not None and (
            not isinstance(notes, str) or len(notes) > 2000
        ):
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "invalid_notes"}
            )
            return
        try:
            result = self.server.registry.decide_memory_candidate(
                candidate_id, decision=decision, notes=notes
            )
        except StoreUnavailable as exc:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "memory_store_unavailable", "message": str(exc)},
            )
            return
        except KeyError:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "candidate_not_found"}
            )
            return
        except ValueError as exc:
            self._send_json(
                HTTPStatus.CONFLICT,
                {"error": "candidate_decision_rejected", "message": str(exc)},
            )
            return
        self._send_json(
            HTTPStatus.OK, {"candidate": result, "decision": decision}
        )

    def _read_json_body(self) -> Optional[Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"}
            )
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return None

    def _stream_events(
        self, run_id: str, state: Optional[LiveRun], since: int
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        seq = since
        try:
            if self.server.registry.has_persistent_event_stream:
                self._stream_persistent_events(run_id, state, seq)
                return
            initial = self.server.registry.stream_events_since(run_id, seq)
            seq = self._write_sse_messages(initial, seq)
            if state is None:
                return
            while True:
                messages, done = state.wait_after(seq, 15.0)
                if messages:
                    seq = self._write_sse_messages(messages, seq)
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                if done and not state.wait_after(seq, 0)[0]:
                    break
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.close_connection = True

    def _stream_persistent_events(
        self, run_id: str, state: Optional[LiveRun], since: int
    ) -> None:
        seq = since
        last_keepalive = time.monotonic()
        while True:
            messages = self.server.registry.stream_events_since(run_id, seq)
            if messages:
                seq = self._write_sse_messages(messages, seq)
                last_keepalive = time.monotonic()
            done = state is None or state.snapshot()["done"]
            if done:
                self.server.registry.flush_observability(0.5)
                final_messages = self.server.registry.stream_events_since(
                    run_id, seq
                )
                if final_messages:
                    seq = self._write_sse_messages(final_messages, seq)
                break
            if time.monotonic() - last_keepalive >= 15.0:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                last_keepalive = time.monotonic()
            threading.Event().wait(0.25)

    def _write_sse_messages(
        self, messages: Iterable[dict], current_seq: int
    ) -> int:
        seq = current_seq
        for message in messages:
            message_seq = _message_sequence(message)
            if message_seq <= seq:
                continue
            payload = json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                default=_json_default,
            )
            frame = f"id: {message_seq}\ndata: {payload}\n\n"
            self.wfile.write(frame.encode("utf-8"))
            seq = message_seq
        self.wfile.flush()
        return seq

    def _query_int(
        self,
        query: dict[str, list[str]],
        name: str,
        default: int,
        *,
        minimum: int,
    ) -> Optional[int]:
        try:
            value = int(query.get(name, [str(default)])[0])
        except (TypeError, ValueError):
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": f"invalid_{name}"}
            )
            return None
        if value < minimum or value > MAX_QUERY_LIMIT:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": f"invalid_{name}"}
            )
            return None
        return value

    def _send_json(self, status: HTTPStatus, value: Any) -> None:
        self._send_bytes(
            status,
            json.dumps(
                value, ensure_ascii=False, default=_json_default
            ).encode("utf-8"),
            "application/json; charset=utf-8",
            cache=False,
        )

    def _send_bytes(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
        *,
        cache: bool = True,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'",
        )
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        LOGGER.info(
            "dashboard request",
            extra={
                "client": self.address_string(),
                "request_message": format % args,
            },
        )


def _parse_run_path(path: str) -> tuple[Optional[str], Optional[str]]:
    parts = [item for item in path.split("/") if item]
    if len(parts) == 3 and parts[:2] == ["api", "runs"]:
        return parts[2], None
    if len(parts) == 4 and parts[:2] == ["api", "runs"]:
        if parts[3] in {
            "events",
            "timeline",
            "trace",
            "logs",
            "errors",
            "metrics",
        }:
            return parts[2], parts[3]
    if (
        len(parts) == 5
        and parts[:2] == ["api", "runs"]
        and parts[3] == "payloads"
        and parts[4]
    ):
        return parts[2], f"payloads/{parts[4]}"
    return None, None


def _parse_candidate_decision_path(path: str) -> Optional[str]:
    parts = [item for item in path.split("/") if item]
    if (
        len(parts) == 5
        and parts[:3] == ["api", "memory", "candidates"]
        and parts[4] == "decision"
    ):
        return parts[3]
    return None


def _require_loopback(host: str) -> None:
    """Reject wildcard or LAN binds; the MVP has no remote auth boundary."""

    candidate = host.strip().lower()
    if candidate == "localhost":
        return
    try:
        if ipaddress.ip_address(candidate).is_loopback:
            return
    except ValueError:
        pass
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ValueError(
            "dashboard host must resolve to a loopback address"
        ) from exc
    if addresses and all(ipaddress.ip_address(item).is_loopback for item in addresses):
        return
    raise ValueError(
        "dashboard only accepts loopback hosts (127.0.0.1 or localhost)"
    )


def _items_from_result(
    result: Any, keys: tuple[str, ...] = ("items",)
) -> list[Any]:
    if result is None:
        return []
    if isinstance(result, dict):
        for key in keys:
            value = result.get(key)
            if isinstance(value, (list, tuple)):
                return list(value)
        return [result]
    if isinstance(result, (list, tuple)):
        return list(result)
    if isinstance(result, Iterable) and not isinstance(
        result, (str, bytes, bytearray)
    ):
        return list(result)
    return [result]


def _as_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        row = dict(value)
    elif dataclasses.is_dataclass(value):
        row = dataclasses.asdict(value)
    else:
        keys = getattr(value, "keys", None)
        if callable(keys):
            row = {key: value[key] for key in keys()}
        else:
            try:
                attributes = vars(value)
            except TypeError:
                return {"value": value}
            row = {
                key: item
                for key, item in attributes.items()
                if not key.startswith("_")
            }
    for key, item in list(row.items()):
        if key.endswith("_json"):
            row.setdefault(key[:-5], _parse_json_value(item))
    return row


def _normalise_stream_message(value: Any) -> dict:
    row = _as_dict(value)
    if "message" in row and isinstance(row["message"], dict):
        row = _as_dict(row["message"])
    name = str(row.get("name") or "")
    category = str(row.get("category") or "")
    if name.startswith("dashboard.stream."):
        row["kind"] = name.removeprefix("dashboard.stream.")
        attributes = row.get("attributes")
        if isinstance(attributes, dict) and attributes.get("dashboard_seq"):
            row["seq"] = attributes["dashboard_seq"]
    elif category == "blackboard":
        row["kind"] = "blackboard_event"
    if "data" not in row:
        payload = row.get("payload")
        if isinstance(payload, str):
            payload = _parse_json_value(payload)
        row["data"] = (
            payload
            if payload is not None
            else row.get("attributes")
            if isinstance(row.get("attributes"), dict)
            else {}
        )
    row.setdefault("kind", row.get("event_type") or row.get("name") or "event")
    row.setdefault(
        "seq",
        row.get("sequence")
        or row.get("offset")
        or row.get("id")
        or 0,
    )
    row.setdefault("at", row.get("timestamp") or row.get("created_at") or 0)
    return row


def _parse_json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _timestamp_sort_value(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _message_sequence(message: dict) -> int:
    raw = (
        message.get("seq")
        or message.get("sequence")
        or message.get("offset")
        or message.get("id")
        or 0
    )
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _json_default(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Path):
        return str(value)
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    try:
        return vars(value)
    except TypeError:
        return str(value)


def create_default_registry() -> RunRegistry:
    """Build the local SQLite-backed dashboard/runtime composition.

    Imports stay local so the HTTP module can still be embedded with test
    doubles or no-op stores.
    """

    observability: Any = None
    memory: Any = None
    try:
        from ..observability import ObservabilityHub

        observability = ObservabilityHub()
    except Exception:
        LOGGER.exception("failed to initialise observability store")
    try:
        from ..memory import SQLiteMemoryStore

        memory = SQLiteMemoryStore()
    except Exception:
        LOGGER.exception("failed to initialise memory store")

    def runtime_factory() -> CoordinationRuntime:
        parameters = inspect.signature(CoordinationRuntime).parameters
        kwargs: dict[str, Any] = {}
        for name in ("observability", "observability_port"):
            if name in parameters and observability is not None:
                kwargs[name] = observability
                break
        for name in ("memory", "memory_port"):
            if name in parameters and memory is not None:
                kwargs[name] = memory
                break
        return CoordinationRuntime(**kwargs)

    return RunRegistry(
        runtime_factory=runtime_factory,
        observability_store=observability,
        memory_store=memory,
        owns_stores=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve the local SwarmBrain coordination dashboard"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be within 1..65535")
    registry = create_default_registry()
    try:
        server = DashboardHTTPServer(
            (args.host, args.port), registry=registry
        )
    except ValueError as exc:
        registry.close()
        parser.error(str(exc))
    print(f"Coordination dashboard: http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        registry.close()


if __name__ == "__main__":
    main()
