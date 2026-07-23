# STATUS: STAGED(A类)—— consumer/session/outbox 的轻量文件持久化
"""协同层私有文件状态。

共享任务事实仍以 Blackboard 为准。本模块只保存消费位置、可恢复 Session、尚未确认
写入的 canonical event 和尚未标准化的原始回执。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..contracts.blackboard_event import BlackboardEvent, EventType, Ledger
from .models import (
    AssignmentSnapshot,
    IntentSession,
    IntentState,
    TaskSession,
    TaskSessionState,
    enum_value,
    to_json_value,
)


def safe_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _storage_root(root: str | Path) -> Path:
    """Resolve a storage root and opt into extended-length paths on Windows."""
    resolved = Path(root).resolve()
    if os.name != "nt":
        return resolved

    value = str(resolved)
    if value.startswith("\\\\?\\"):
        return resolved
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    encoded = json.dumps(
        to_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def event_to_dict(event: BlackboardEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "type": enum_value(event.type),
        "ledger": enum_value(event.ledger),
        "content": to_json_value(event.content),
        "source": event.source,
        # ts/version/offset 由 Blackboard 分配，不属于 canonical business event。
        "ts": 0.0,
        "confidence": event.confidence,
        "ttl": event.ttl,
        "version": 0,
        "idempotency_key": event.idempotency_key,
        "extra": to_json_value(event.extra),
    }


def event_from_dict(value: dict[str, Any]) -> BlackboardEvent:
    event_type_value = str(value["type"])
    try:
        event_type: Any = EventType(event_type_value)
    except ValueError:
        event_type = event_type_value
    return BlackboardEvent(
        id=str(value["id"]),
        type=event_type,
        ledger=Ledger(str(value["ledger"])),
        content=dict(value.get("content", {})),
        source=str(value["source"]),
        ts=float(value.get("ts", 0.0)),
        confidence=value.get("confidence"),
        ttl=value.get("ttl"),
        version=int(value.get("version", 0)),
        idempotency_key=value.get("idempotency_key"),
        extra=dict(value.get("extra", {})),
    )


@dataclass(frozen=True)
class OutboxRecord:
    event: BlackboardEvent
    source_offset: Optional[int]


def _record_to_dict(event: BlackboardEvent, source_offset: Optional[int]) -> dict[str, Any]:
    return {
        "event": event_to_dict(event),
        "source_offset": source_offset,
    }


def _record_from_dict(value: dict[str, Any]) -> OutboxRecord:
    # 兼容首版直接保存 Event 的 outbox 文件。
    if isinstance(value.get("event"), dict):
        event_value = value["event"]
        source_offset = value.get("source_offset")
    else:
        event_value = value
        source_offset = None
    return OutboxRecord(
        event=event_from_dict(event_value),
        source_offset=(int(source_offset) if source_offset is not None else None),
    )


class FileOutboxStore:
    """A process-private durable outbox plus consumer offset."""

    def __init__(self, root: str | Path) -> None:
        self.root = _storage_root(root)
        self.outbox_root = self.root / "outbox"
        self.consumer_file = self.root / "consumer.json"
        self._lock = threading.RLock()
        self.outbox_root.mkdir(parents=True, exist_ok=True)

    def load_consumer_offset(self) -> int:
        with self._lock:
            value = _read_json(self.consumer_file, {"last_offset": -1})
            return int(value.get("last_offset", -1))

    def save_consumer_offset(self, offset: int) -> None:
        with self._lock:
            previous = self.load_consumer_offset()
            if offset < previous:
                raise ValueError("consumer offset cannot move backwards")
            _atomic_write_json(self.consumer_file, {"last_offset": int(offset)})

    def stage_event(
        self, event: BlackboardEvent, source_offset: Optional[int] = None
    ) -> BlackboardEvent:
        if not event.idempotency_key:
            raise ValueError("outbox event requires idempotency_key")
        key = safe_key(event.idempotency_key)
        path = self.outbox_root / f"{key}.json"
        rejected_path = self.root / "rejected" / f"{key}.json"
        payload = _record_to_dict(event, source_offset)
        with self._lock:
            if rejected_path.exists():
                raise ValueError("OUTBOX_EVENT_PREVIOUSLY_REJECTED")
            existing = _read_json(path, None)
            if existing is None:
                _atomic_write_json(path, payload)
                return event_from_dict(payload["event"])
            # 重启后必须复用第一次持久化的 canonical event；不能用新的当前时间
            # 重新组装 deadline/completed_at 等字段。
            return _record_from_dict(existing).event

    def list_events(self) -> list[BlackboardEvent]:
        return [record.event for record in self.list_event_records()]

    def list_event_records(self) -> list[OutboxRecord]:
        with self._lock:
            paths = sorted(self.outbox_root.glob("*.json"), key=lambda item: item.name)
            return [_record_from_dict(_read_json(path, {})) for path in paths]

    def resolve_event(self, idempotency_key: str) -> None:
        path = self.outbox_root / f"{safe_key(idempotency_key)}.json"
        with self._lock:
            if path.exists():
                path.unlink()

    def has_pending_events(self) -> bool:
        with self._lock:
            return any(self.outbox_root.glob("*.json"))

    def reject_event(self, idempotency_key: str, result: Any) -> None:
        """Quarantine a permanent producer error so startup replay cannot loop on it."""
        key = safe_key(idempotency_key)
        path = self.outbox_root / f"{key}.json"
        rejected_path = self.root / "rejected" / f"{key}.json"
        with self._lock:
            value = _read_json(path, None)
            if value is None:
                return
            value["append_rejection"] = to_json_value(result)
            _atomic_write_json(rejected_path, value)
            path.unlink()


class FileTaskSessionStore(FileOutboxStore):
    """One AgentProcess private state rooted by a safe device key."""

    def __init__(self, work_root: str | Path, device_id: str) -> None:
        self.device_id = device_id
        process_root = Path(work_root).resolve() / "agents" / safe_key(device_id)
        super().__init__(process_root)
        self.tasks_root = self.root / "tasks"
        self.tasks_root.mkdir(parents=True, exist_ok=True)

    def _task_root(self, task_id: str) -> Path:
        return self.tasks_root / safe_key(task_id)

    def load_session(self, task_id: str) -> Optional[TaskSession]:
        path = self._task_root(task_id) / "session.json"
        with self._lock:
            value = _read_json(path, None)
        if value is None:
            return None
        assignment_value = value.get("assignment")
        intent_value = value.get("current_intent")
        return TaskSession(
            task_id=str(value["task_id"]),
            task_revision=int(value["task_revision"]),
            coordination_epoch=int(value["coordination_epoch"]),
            state=TaskSessionState(str(value["state"])),
            assignment=(
                AssignmentSnapshot.from_dict(assignment_value)
                if isinstance(assignment_value, dict)
                else None
            ),
            current_intent=(
                IntentSession(
                    intent_id=str(intent_value["intent_id"]),
                    state=IntentState(str(intent_value["state"])),
                    intent_fingerprint=str(intent_value["intent_fingerprint"]),
                    receipt_id=intent_value.get("receipt_id"),
                )
                if isinstance(intent_value, dict)
                else None
            ),
            last_receipt_id=value.get("last_receipt_id"),
            session_version=int(value.get("session_version", 1)),
        )

    def save_session(self, session: TaskSession) -> None:
        path = self._task_root(session.task_id) / "session.json"
        with self._lock:
            current = _read_json(path, None)
            if current is not None:
                current_version = int(current.get("session_version", 0))
                if current_version > session.session_version:
                    raise ValueError("session version cannot move backwards")
                if current_version == session.session_version and current != to_json_value(session):
                    raise ValueError("same session version cannot overwrite different content")
            _atomic_write_json(path, session)

    def list_sessions(self) -> list[TaskSession]:
        sessions: list[TaskSession] = []
        with self._lock:
            paths = sorted(self.tasks_root.glob("*/session.json"), key=lambda item: item.parent.name)
        for path in paths:
            value = _read_json(path, None)
            if not isinstance(value, dict) or not value.get("task_id"):
                continue
            session = self.load_session(str(value["task_id"]))
            if session is not None:
                sessions.append(session)
        return sessions

    def stage_task_event(
        self,
        task_id: str,
        event: BlackboardEvent,
        source_offset: Optional[int] = None,
    ) -> BlackboardEvent:
        if not event.idempotency_key:
            raise ValueError("outbox event requires idempotency_key")
        key = safe_key(event.idempotency_key)
        path = self._task_root(task_id) / "outbox" / f"{key}.json"
        rejected_path = self._task_root(task_id) / "rejected" / f"{key}.json"
        payload = _record_to_dict(event, source_offset)
        with self._lock:
            if rejected_path.exists():
                raise ValueError("OUTBOX_EVENT_PREVIOUSLY_REJECTED")
            existing = _read_json(path, None)
            if existing is None:
                _atomic_write_json(path, payload)
                return event_from_dict(payload["event"])
            return _record_from_dict(existing).event

    def list_task_events(self, task_id: str) -> list[BlackboardEvent]:
        return [record.event for record in self.list_task_event_records(task_id)]

    def list_task_event_records(self, task_id: str) -> list[OutboxRecord]:
        root = self._task_root(task_id) / "outbox"
        with self._lock:
            return [
                _record_from_dict(_read_json(path, {}))
                for path in sorted(root.glob("*.json"), key=lambda item: item.name)
            ]

    def list_all_task_event_records(self) -> list[OutboxRecord]:
        """Scan task directories directly; an outbox may exist before session.json."""
        with self._lock:
            paths = sorted(
                self.tasks_root.glob("*/outbox/*.json"), key=lambda item: str(item)
            )
            return [_record_from_dict(_read_json(path, {})) for path in paths]

    def resolve_task_event(self, task_id: str, idempotency_key: str) -> None:
        path = self._task_root(task_id) / "outbox" / f"{safe_key(idempotency_key)}.json"
        with self._lock:
            if path.exists():
                path.unlink()

    def reject_task_event(self, task_id: str, idempotency_key: str, result: Any) -> None:
        key = safe_key(idempotency_key)
        path = self._task_root(task_id) / "outbox" / f"{key}.json"
        rejected_path = self._task_root(task_id) / "rejected" / f"{key}.json"
        with self._lock:
            value = _read_json(path, None)
            if value is None:
                return
            value["append_rejection"] = to_json_value(result)
            _atomic_write_json(rejected_path, value)
            path.unlink()

    def store_raw_receipt(self, task_id: str, intent_id: str, receipt: Any) -> None:
        path = self._task_root(task_id) / "raw_receipts" / f"{safe_key(intent_id)}.json"
        with self._lock:
            _atomic_write_json(path, receipt)

    def remove_raw_receipt(self, task_id: str, intent_id: str) -> None:
        path = self._task_root(task_id) / "raw_receipts" / f"{safe_key(intent_id)}.json"
        with self._lock:
            if path.exists():
                path.unlink()

    def cleanup_task(self, task_id: str) -> bool:
        """Remove a terminal task only after its task-local durable files converge."""
        root = self._task_root(task_id)
        with self._lock:
            if any((root / "outbox").glob("*.json")):
                return False
            if any((root / "raw_receipts").glob("*.json")):
                return False
            if not root.exists():
                return True
            for child in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            root.rmdir()
            return True


class FileCoordinatorStore(FileOutboxStore):
    def __init__(self, work_root: str | Path) -> None:
        super().__init__(Path(work_root).resolve() / "coordinator")
