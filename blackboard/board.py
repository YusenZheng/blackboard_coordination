# STATUS: STAGED(A类)—— coordination v2 可直接接入的单进程权威内存黑板
"""线程安全的 append-only Blackboard。

本实现同时提供：

* coordination v2 使用的结构化 append、exclusive cursor、全局水位和窄视图；
* v1 Harness 使用的 read_events/subscribe/claim/release 兼容入口；
* Registry/Telemetry 快照到 ``agent_public`` 视图的只读适配口。

它仍是单进程内存实现，不声称跨进程持久化或 exactly-once。
"""
from __future__ import annotations

import copy
import json
import math
import threading
import time
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Optional

from ..contracts.blackboard_event import (
    AppendResult,
    AppendStatus,
    BlackboardEvent,
    EventType,
    Ledger,
    StoredEvent,
)
from .validation import is_v2_content, validate_event_contract


_EXPECTED_LEDGERS = {
    "task_posted": Ledger.TASK.value,
    "bid_round_opened": Ledger.TASK.value,
    "bid": Ledger.TASK.value,
    "handoff": Ledger.TASK.value,
    "ask_help": Ledger.TASK.value,
    "task_assigned": Ledger.TASK.value,
    "assignment_completed": Ledger.TASK.value,
    "task_replan": Ledger.TASK.value,
    "task_done": Ledger.TASK.value,
    "task_failed": Ledger.TASK.value,
    "clue": Ledger.EVIDENCE.value,
    "fusion": Ledger.EVIDENCE.value,
    "action_intent": Ledger.RECEIPT.value,
    "receipt": Ledger.RECEIPT.value,
    "safety_intercept": Ledger.RECEIPT.value,
    "estop": Ledger.RECEIPT.value,
    "claim": Ledger.RECEIPT.value,
    "yield": Ledger.RECEIPT.value,
    "auth_point": Ledger.HUMAN.value,
    "auth_decision": Ledger.HUMAN.value,
}
_TERMINAL_TYPES = {"task_done", "task_failed"}
_STATE_GATED_V2_TYPES = {
    "task_posted",
    "bid_round_opened",
    "task_assigned",
    "action_intent",
    "task_replan",
    "task_done",
    "task_failed",
}
_LEGACY_STATE_TYPES_BLOCKED_FOR_V2_TASKS = {
    "task_assigned",
    "safety_intercept",
    "task_done",
}


def _value(value: Any) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, set):
        return [_json_value(item) for item in sorted(value, key=repr)]
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _canonical_event(event: BlackboardEvent) -> str:
    """Canonical business identity; server-owned ts/version are deliberately absent."""

    value = {
        "id": event.id,
        "type": _value(event.type),
        "ledger": _value(event.ledger),
        "content": _json_value(event.content),
        "source": event.source,
        "confidence": event.confidence,
        "ttl": event.ttl,
        "idempotency_key": event.idempotency_key,
        "extra": _json_value(event.extra),
    }
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    )


def _terminal_key(event: BlackboardEvent) -> Optional[tuple[str, int]]:
    if _value(event.type) not in _TERMINAL_TYPES:
        return None
    task_id = str(event.content.get("task_id", ""))
    if not task_id:
        return None
    try:
        revision = int(event.content.get("task_revision", 1))
    except (TypeError, ValueError):
        return None
    return task_id, revision


class Blackboard:
    """单进程权威事件流；所有提交和索引更新在同一把锁内完成。"""

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], float]] = None,
        agent_snapshot_provider: Any = None,
    ) -> None:
        self._clock = clock or time.time
        self._events: list[BlackboardEvent] = []
        self._idempotency_index: dict[str, StoredEvent] = {}
        self._event_id_index: dict[str, StoredEvent] = {}
        self._terminal_index: dict[tuple[str, int], StoredEvent] = {}
        self._subscribers: list[tuple[str, Callable, Optional[set[str]]]] = []
        self._subscriber_errors: dict[str, str] = {}
        self._leases: dict[str, str] = {}
        self._sub_id = 0
        self._agent_snapshot_provider = agent_snapshot_provider
        self._agent_snapshots: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)

    # ------------------------------------------------------------------
    # coordination v2 authority API
    # ------------------------------------------------------------------
    def now(self) -> float:
        return float(self._clock())

    def high_watermark(self) -> int:
        """Return the global event-version watermark (0 for an empty stream)."""

        with self._lock:
            return len(self._events)

    def append(self, event: BlackboardEvent) -> AppendResult:
        """Atomically append or return an explicit idempotency/conflict result."""

        try:
            candidate = copy.deepcopy(event)
        except Exception:
            return AppendResult(
                AppendStatus.REJECTED,
                None,
                None,
                str(getattr(event, "id", "")),
                reason_code="EVENT_NOT_COPYABLE",
            )
        rejection = self._validate_event(candidate)
        if rejection is not None:
            return AppendResult(
                AppendStatus.REJECTED,
                None,
                None,
                str(getattr(candidate, "id", "")),
                reason_code=rejection,
            )

        canonical = _canonical_event(candidate)
        terminal_key = _terminal_key(candidate)
        subscribers: list[tuple[str, Callable, Optional[set[str]]]] = []

        with self._changed:
            if candidate.idempotency_key:
                existing = self._idempotency_index.get(candidate.idempotency_key)
                if existing is not None:
                    if canonical == _canonical_event(existing.event):
                        self._stamp_caller(event, existing.event)
                        return self._result_for_existing(
                            AppendStatus.DUPLICATE_SAME, existing, existing.event.id
                        )
                    if (
                        terminal_key is not None
                        and terminal_key == _terminal_key(existing.event)
                        and _value(existing.event.type) in _TERMINAL_TYPES
                    ):
                        return self._result_for_existing(
                            AppendStatus.TERMINAL_CONFLICT,
                            existing,
                            candidate.id,
                            "TASK_TERMINAL_ALREADY_COMMITTED",
                        )
                    return self._result_for_existing(
                        AppendStatus.KEY_CONFLICT,
                        existing,
                        candidate.id,
                        "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_EVENT",
                    )

            if terminal_key is not None:
                existing = self._terminal_index.get(terminal_key)
                if existing is not None:
                    if canonical == _canonical_event(existing.event):
                        self._stamp_caller(event, existing.event)
                        return self._result_for_existing(
                            AppendStatus.DUPLICATE_SAME, existing, existing.event.id
                        )
                    return self._result_for_existing(
                        AppendStatus.TERMINAL_CONFLICT,
                        existing,
                        candidate.id,
                        "TASK_TERMINAL_ALREADY_COMMITTED",
                    )

            existing_by_id = self._event_id_index.get(candidate.id)
            if existing_by_id is not None:
                if canonical == _canonical_event(existing_by_id.event):
                    self._stamp_caller(event, existing_by_id.event)
                    return self._result_for_existing(
                        AppendStatus.DUPLICATE_SAME,
                        existing_by_id,
                        existing_by_id.event.id,
                    )
                return self._result_for_existing(
                    AppendStatus.KEY_CONFLICT,
                    existing_by_id,
                    candidate.id,
                    "EVENT_ID_REUSED_WITH_DIFFERENT_EVENT",
                )

            transition_rejection = self._validate_v2_transition_locked(candidate)
            if transition_rejection is not None:
                return AppendResult(
                    AppendStatus.REJECTED,
                    None,
                    None,
                    candidate.id,
                    reason_code=transition_rejection,
                )

            offset = len(self._events)
            version = offset + 1
            committed_at = self.now()
            if committed_at < 0:
                return AppendResult(
                    AppendStatus.REJECTED,
                    None,
                    None,
                    candidate.id,
                    reason_code="NEGATIVE_SERVER_TIME",
                )
            candidate.version = version
            candidate.ts = committed_at
            self._events.append(candidate)
            stored = StoredEvent(offset, candidate)
            self._event_id_index[candidate.id] = stored
            if candidate.idempotency_key:
                self._idempotency_index[candidate.idempotency_key] = stored
            if terminal_key is not None:
                self._terminal_index[terminal_key] = stored
            self._stamp_caller(event, candidate)
            subscribers = list(self._subscribers)
            self._changed.notify_all()
            result = AppendResult(
                AppendStatus.APPENDED,
                offset,
                version,
                candidate.id,
            )

        # The event is already committed. Subscriber failures cannot roll it back or
        # prevent delivery to other subscribers. Synchronous delivery is retained for
        # the legacy walking skeleton, which reads bids immediately after TASK_POSTED.
        for sid, handler, event_types in subscribers:
            if event_types is not None and _value(candidate.type) not in event_types:
                continue
            try:
                handler(copy.deepcopy(candidate), offset)
            except Exception as exc:  # pragma: no cover - observable diagnostic path
                with self._lock:
                    self._subscriber_errors[sid] = f"{type(exc).__name__}: {exc}"
        return result

    def read_since(
        self,
        offset: int,
        event_types: Optional[set[str]],
        limit: int,
        timeout_s: float,
    ) -> list[StoredEvent]:
        """Return events with offsets strictly greater than ``offset``."""

        if offset < -1:
            raise ValueError("last_offset must be >= -1")
        if limit <= 0:
            raise ValueError("limit must be positive")
        if timeout_s < 0:
            raise ValueError("timeout_s must be non-negative")
        requested = (
            {_value(item) for item in event_types}
            if event_types is not None
            else None
        )
        deadline = time.monotonic() + timeout_s
        with self._changed:
            while True:
                values = self._read_since_locked(offset, requested, limit)
                if values or timeout_s == 0:
                    return values
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._changed.wait(remaining)

    def query_view(
        self,
        ledger: Optional[Ledger | str] = None,
        filt: Optional[dict] = None,
        min_version: Optional[int] = None,
        timeout_s: float = 0.0,
    ) -> dict | list[dict]:
        """Return a view folded at one consistent global watermark."""

        if min_version is not None and min_version < 0:
            raise ValueError("min_version must be non-negative")
        if timeout_s < 0:
            raise ValueError("timeout_s must be non-negative")
        deadline = time.monotonic() + timeout_s
        with self._changed:
            while min_version is not None and len(self._events) < min_version:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
            watermark = len(self._events)
            events = copy.deepcopy(self._events)
            explicit_snapshots = copy.deepcopy(self._agent_snapshots)
            provider = self._agent_snapshot_provider

        query = dict(filt or {})
        agent_snapshots = explicit_snapshots
        if query.get("view_type") == "agent_public":
            provided = self._load_agent_snapshots(provider, query)
            provided.update(explicit_snapshots)  # explicit upserts are authoritative
            agent_snapshots = provided

        from .view import fold_view

        return fold_view(
            events,
            ledger=ledger,
            filt=query,
            view_version=watermark,
            now=self.now(),
            agent_snapshots=agent_snapshots,
        )

    # ------------------------------------------------------------------
    # AgentCard / telemetry snapshot bridge (not a fifth event ledger)
    # ------------------------------------------------------------------
    def set_agent_snapshot_provider(self, provider: Any) -> None:
        """Inject Registry-like ``get/all_cards`` or a zero-arg snapshot provider."""

        with self._lock:
            self._agent_snapshot_provider = provider

    def upsert_agent_snapshot(self, snapshot: Any) -> dict[str, Any]:
        """Publish a low-frequency public snapshot without writing telemetry events."""

        value = self._normalize_agent_snapshot(snapshot)
        device_id = str(value.get("device_id", ""))
        if not device_id:
            raise ValueError("agent snapshot requires device_id")
        with self._lock:
            previous = self._agent_snapshots.get(device_id, {})
            if int(value.get("card_version", 0)) <= 0:
                value["card_version"] = int(previous.get("card_version", 0)) + 1
            self._agent_snapshots[device_id] = copy.deepcopy(value)
        return copy.deepcopy(value)

    def remove_agent_snapshot(self, device_id: str) -> None:
        with self._lock:
            self._agent_snapshots.pop(device_id, None)

    # ------------------------------------------------------------------
    # v1 compatibility API
    # ------------------------------------------------------------------
    def read_events(
        self, since_offset: int = 0, types: Optional[list] = None
    ) -> list[BlackboardEvent]:
        """Legacy inclusive-offset read returning bare events."""

        if since_offset < 0:
            raise ValueError("since_offset must be non-negative")
        requested = {_value(item) for item in types} if types is not None else None
        with self._lock:
            values = self._events[since_offset:]
            if requested is not None:
                values = [item for item in values if _value(item.type) in requested]
            return copy.deepcopy(values)

    def subscribe(
        self,
        handler: Callable[[BlackboardEvent, int], None],
        types: Optional[list] = None,
        from_offset: int = 0,
    ) -> str:
        """Legacy synchronous subscription with historical catch-up."""

        if from_offset < 0:
            raise ValueError("from_offset must be non-negative")
        requested = {_value(item) for item in types} if types is not None else None
        with self._lock:
            self._sub_id += 1
            sid = f"sub-{self._sub_id}"
            self._subscribers.append((sid, handler, requested))
            history = [
                (offset, copy.deepcopy(event))
                for offset, event in enumerate(self._events)
                if offset >= from_offset
                and (requested is None or _value(event.type) in requested)
            ]
        for offset, event in history:
            try:
                handler(event, offset)
            except Exception as exc:  # pragma: no cover - diagnostic path
                with self._lock:
                    self._subscriber_errors[sid] = f"{type(exc).__name__}: {exc}"
        return sid

    def unsubscribe(self, subscription_id: str) -> bool:
        """Remove one synchronous subscriber without disturbing other consumers."""
        with self._lock:
            before = len(self._subscribers)
            self._subscribers = [
                item for item in self._subscribers if item[0] != subscription_id
            ]
            self._subscriber_errors.pop(subscription_id, None)
            return len(self._subscribers) != before

    def claim(self, resource: str, device_id: str) -> bool:
        with self._lock:
            holder = self._leases.get(resource)
            if holder is None:
                self._leases[resource] = device_id
                return True
            return holder == device_id

    def release(self, resource: str, device_id: str) -> None:
        with self._lock:
            if self._leases.get(resource) == device_id:
                del self._leases[resource]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_event(event: Any) -> Optional[str]:
        if not isinstance(event, BlackboardEvent):
            return "EVENT_TYPE_INVALID"
        if not isinstance(event.id, str) or not event.id:
            return "EVENT_ID_REQUIRED"
        if not isinstance(event.source, str) or not event.source:
            return "EVENT_SOURCE_REQUIRED"
        if not isinstance(event.content, dict):
            return "EVENT_CONTENT_MUST_BE_OBJECT"
        if event.idempotency_key is not None and (
            not isinstance(event.idempotency_key, str) or not event.idempotency_key
        ):
            return "IDEMPOTENCY_KEY_INVALID"
        if not isinstance(event.extra, dict):
            return "EVENT_EXTRA_MUST_BE_OBJECT"
        event_type = _value(event.type)
        ledger = _value(event.ledger)
        if event_type not in {item.value for item in EventType}:
            return "EVENT_TYPE_UNKNOWN"
        if ledger not in {item.value for item in Ledger}:
            return "LEDGER_INVALID"
        expected = _EXPECTED_LEDGERS.get(event_type)
        if expected is not None and ledger != expected:
            return "EVENT_LEDGER_MISMATCH"
        try:
            if event.confidence is not None:
                confidence = float(event.confidence)
                if not math.isfinite(confidence):
                    return "CONFIDENCE_INVALID"
                if not 0.0 <= confidence <= 1.0:
                    return "CONFIDENCE_OUT_OF_RANGE"
        except (TypeError, ValueError):
            return "CONFIDENCE_INVALID"
        try:
            if event.ttl is not None:
                ttl = float(event.ttl)
                if not math.isfinite(ttl):
                    return "TTL_INVALID"
                if ttl < 0:
                    return "TTL_NEGATIVE"
        except (TypeError, ValueError):
            return "TTL_INVALID"
        if _terminal_key(event) is None and event_type in _TERMINAL_TYPES:
            return "TERMINAL_TASK_ID_OR_REVISION_INVALID"
        return validate_event_contract(event.type, event.content)

    def _validate_v2_transition_locked(
        self, event: BlackboardEvent
    ) -> Optional[str]:
        """Reject stale v2 state transitions before they can occupy indexes.

        Late facts that do not claim state ownership (for example CLUE, BID,
        RECEIPT, SAFETY_INTERCEPT and ASSIGNMENT_COMPLETED) remain appendable and
        are isolated by their views. Ownership transitions are checked against a
        fold of the committed prefix. In particular, a stale terminal must not
        win the task-level terminal key and block the real current terminal.
        """

        event_type = _value(event.type)
        if not is_v2_content(event.content):
            if event_type not in _LEGACY_STATE_TYPES_BLOCKED_FOR_V2_TASKS:
                return None
            task_id = str(event.content.get("task_id", ""))
            if task_id and any(
                _value(existing.type) == EventType.TASK_POSTED.value
                and str(existing.content.get("task_id", "")) == task_id
                and is_v2_content(existing.content)
                for existing in self._events
            ):
                return "SCHEMA_VERSION_REQUIRED"
            return None
        if event_type not in _STATE_GATED_V2_TYPES:
            return None
        content = event.content
        task_id = str(content.get("task_id", ""))
        revision = int(content.get("task_revision", 1))

        if event_type == EventType.TASK_POSTED.value:
            for existing in self._events:
                if (
                    _value(existing.type) == EventType.TASK_POSTED.value
                    and str(existing.content.get("task_id", "")) == task_id
                    and int(existing.content.get("task_revision", 1)) == revision
                ):
                    return "TASK_REVISION_ALREADY_POSTED"
            return None
        from .view import fold_view

        task_view = fold_view(
            self._events,
            ledger=Ledger.TASK,
            filt={
                "view_type": "task_coordination",
                "task_id": task_id,
                "task_revision": revision,
            },
            view_version=len(self._events),
            now=self.now(),
        )
        if not task_view.get("task"):
            return "TASK_REVISION_NOT_FOUND"
        if task_view.get("terminal_event_id") is not None:
            return "TASK_ALREADY_TERMINAL"

        current_epoch = int(task_view.get("coordination_epoch", 0))
        active_round = task_view.get("active_bid_round") or {}
        current_plan = task_view.get("current_plan") or {}

        if event_type == EventType.BID_ROUND_OPENED.value:
            epoch = int(content["coordination_epoch"])
            expected_epoch = 1 if current_epoch == 0 else current_epoch
            if epoch != expected_epoch:
                return "COORDINATION_EPOCH_MISMATCH"
            if current_plan:
                return "TASK_PLAN_ALREADY_COMMITTED"
            if active_round:
                active_epoch = int(active_round.get("coordination_epoch", 0))
                active_number = int(active_round.get("bid_round", 0))
                if epoch == active_epoch and int(content["bid_round"]) <= active_number:
                    return "BID_ROUND_NOT_ADVANCING"
            return None

        if event_type == EventType.TASK_ASSIGNED.value:
            if int(content["coordination_epoch"]) != current_epoch:
                return "COORDINATION_EPOCH_MISMATCH"
            if not active_round:
                return "BID_ROUND_NOT_ACTIVE"
            if current_plan:
                return "TASK_PLAN_ALREADY_COMMITTED"
            return None

        if event_type == EventType.ACTION_INTENT.value:
            error = self._validate_plan_context(content, current_epoch, current_plan)
            if error is not None:
                return error
            return None

        if event_type == EventType.TASK_REPLAN.value:
            if int(content["from_epoch"]) != current_epoch:
                return "COORDINATION_EPOCH_MISMATCH"
            if int(content["to_epoch"]) != current_epoch + 1:
                return "EPOCH_RELATION_INVALID"
            pending = set(task_view.get("pending_replan_cause_ids", []))
            if content["cause_event_id"] not in pending:
                return "REPLAN_CAUSE_NOT_PENDING"
            if not set(content["evidence_refs"]).issubset(pending):
                return "REPLAN_EVIDENCE_NOT_PENDING"
            return None

        if event_type == EventType.TASK_DONE.value:
            if int(content["coordination_epoch"]) != current_epoch:
                return "COORDINATION_EPOCH_MISMATCH"
            if not current_plan or content["plan_id"] != current_plan.get("plan_id"):
                return "TASK_DONE_PLAN_MISMATCH"
            expected = {
                str(item.get("assignment_id", ""))
                for item in current_plan.get("assignments", [])
                if item.get("assignment_id")
            }
            completed = set(task_view.get("completed_assignment_ids", []))
            declared = set(content["completed_assignment_ids"])
            if not expected or not expected.issubset(completed) or declared != completed:
                return "TASK_DONE_COMPLETION_INCOMPLETE"
            return None

        if event_type == EventType.TASK_FAILED.value:
            if int(content["coordination_epoch"]) != current_epoch:
                return "COORDINATION_EPOCH_MISMATCH"
            return None
        return None

    @staticmethod
    def _validate_plan_context(
        content: dict[str, Any], current_epoch: int, current_plan: dict[str, Any]
    ) -> Optional[str]:
        if int(content["coordination_epoch"]) != current_epoch:
            return "COORDINATION_EPOCH_MISMATCH"
        if not current_plan or content["plan_id"] != current_plan.get("plan_id"):
            return "PLAN_CONTEXT_MISMATCH"
        assignment_id = str(content.get("assignment_id", ""))
        assignment = next(
            (
                item
                for item in current_plan.get("assignments", [])
                if str(item.get("assignment_id", "")) == assignment_id
            ),
            None,
        )
        if assignment is None:
            return "ASSIGNMENT_CONTEXT_MISMATCH"
        if int(content["assignment_epoch"]) != int(
            assignment.get("assignment_epoch", -1)
        ):
            return "ASSIGNMENT_CONTEXT_MISMATCH"
        return None

    @staticmethod
    def _stamp_caller(target: Any, committed: BlackboardEvent) -> None:
        try:
            target.version = committed.version
            target.ts = committed.ts
        except Exception:
            pass

    @staticmethod
    def _result_for_existing(
        status: AppendStatus,
        existing: StoredEvent,
        event_id: str,
        reason_code: Optional[str] = None,
    ) -> AppendResult:
        return AppendResult(
            status,
            existing.offset,
            int(existing.event.version),
            event_id,
            existing_event_id=existing.event.id,
            reason_code=reason_code,
        )

    def _read_since_locked(
        self,
        offset: int,
        requested: Optional[set[str]],
        limit: int,
    ) -> list[StoredEvent]:
        result: list[StoredEvent] = []
        for current_offset in range(offset + 1, len(self._events)):
            event = self._events[current_offset]
            if requested is not None and _value(event.type) not in requested:
                continue
            result.append(StoredEvent(current_offset, copy.deepcopy(event)))
            if len(result) >= limit:
                break
        return result

    def _load_agent_snapshots(
        self, provider: Any, query: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        if provider is None:
            return {}
        requested: Optional[list[str]] = None
        if query.get("device_id") is not None:
            requested = [str(query["device_id"])]
        elif isinstance(query.get("device_ids"), (list, tuple, set)):
            requested = [str(item) for item in query["device_ids"]]

        sources: list[Any] = []
        if requested is not None and hasattr(provider, "get"):
            sources = [provider.get(device_id) for device_id in requested]
        elif hasattr(provider, "all_cards"):
            sources = list(provider.all_cards())
        elif callable(provider):
            produced = provider()
            if isinstance(produced, dict):
                if "device_id" in produced or "identity" in produced:
                    sources = [produced]
                else:
                    sources = list(produced.values())
            elif isinstance(produced, Iterable) and not isinstance(produced, (str, bytes)):
                sources = list(produced)
            elif produced is not None:
                sources = [produced]

        values: dict[str, dict[str, Any]] = {}
        for source in sources:
            if source is None:
                continue
            value = self._normalize_agent_snapshot(source)
            device_id = str(value.get("device_id", ""))
            if device_id:
                values[device_id] = value
        return values

    def _normalize_agent_snapshot(self, source: Any) -> dict[str, Any]:
        raw = _json_value(source)
        if not isinstance(raw, dict):
            raise TypeError("agent snapshot provider returned a non-object")

        # Already in AgentPublicSnapshot shape.
        if "device_id" in raw and "identity" not in raw:
            value = copy.deepcopy(raw)
            value.setdefault("state_updated_at", self.now())
            value.setdefault("card_version", 1)
            return value

        identity = dict(raw.get("identity", {}))
        state = dict(raw.get("state", {}))
        capability = dict(raw.get("capability", {}))
        profile = dict(capability.get("profile", {}))
        resume = dict(raw.get("resume", {}))
        state_extra = dict(state.get("extra", {}))
        card_extra = dict(raw.get("extra", {}))
        position = state.get("position")
        position_stamp = (
            float(position.get("stamp", 0.0)) if isinstance(position, dict) else 0.0
        )
        updated_at = (
            state_extra.get("state_updated_at")
            or state_extra.get("updated_at")
            or card_extra.get("state_updated_at")
            or position_stamp
            or self.now()
        )
        tasks_total = int(resume.get("tasks_total", 0))
        success_rate = (
            float(resume.get("tasks_done", 0)) / tasks_total if tasks_total else 0.0
        )
        return {
            "device_id": str(identity.get("device_id", "")),
            "device_type": _value(identity.get("device_type", "other")),
            "online": bool(state.get("online", False)),
            "healthy": bool(state.get("healthy", False)),
            "busy": bool(state.get("busy", False)),
            "busy_task_id": state_extra.get("busy_task_id"),
            "battery": float(state.get("battery", 0.0)),
            "endurance_s": float(state.get("endurance_s", 0.0)),
            "position": copy.deepcopy(position),
            "current_action": str(state.get("current_action", "")),
            "capabilities": list(profile.get("capabilities", [])),
            "action_verbs": [_value(item) for item in capability.get("action_verbs", [])],
            "constraints": list(profile.get("constraints", [])),
            "width_cm": profile.get("width_cm"),
            "lowest_posture_cm": profile.get("lowest_posture_cm"),
            "step_limit_cm": profile.get("step_limit_cm"),
            "payload_kg": profile.get("payload_kg"),
            "success_rate": success_rate,
            "card_version": int(card_extra.get("card_version", 1)),
            "state_updated_at": float(updated_at),
        }
