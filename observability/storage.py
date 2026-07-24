"""SQLite WAL 存储、非阻塞有界写队列及应急 JSONL。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .redaction import redact_value


SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path("runtime_data") / "observability.sqlite3"
DEFAULT_EMERGENCY_PATH = Path("runtime_data") / "logs" / "emergency.jsonl"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(
        redact_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _loads(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


@dataclass(frozen=True)
class _QueuedRecord:
    kind: str
    payload: dict[str, Any]
    tier: str


class _TieredQueue:
    """满载时按 debug/payload → normal → critical 的顺序牺牲记录。"""

    _TIERS = ("critical", "normal", "debug")

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("queue capacity must be positive")
        self.capacity = capacity
        self._queues: dict[str, deque[_QueuedRecord]] = {
            tier: deque() for tier in self._TIERS
        }
        self._condition = threading.Condition()
        self._size = 0
        self._unfinished = 0
        self._closed = False
        self._dropped = {tier: 0 for tier in self._TIERS}

    def put(self, record: _QueuedRecord) -> bool:
        tier = record.tier if record.tier in self._queues else "normal"
        with self._condition:
            if self._closed:
                self._dropped[tier] += 1
                return False
            if self._size >= self.capacity:
                evicted_tier: str | None = None
                if tier == "critical":
                    if self._queues["debug"]:
                        evicted_tier = "debug"
                    elif self._queues["normal"]:
                        evicted_tier = "normal"
                elif tier == "normal" and self._queues["debug"]:
                    evicted_tier = "debug"
                if evicted_tier is None:
                    self._dropped[tier] += 1
                    return False
                self._queues[evicted_tier].popleft()
                self._size -= 1
                self._unfinished -= 1
                self._dropped[evicted_tier] += 1
            self._queues[tier].append(record)
            self._size += 1
            self._unfinished += 1
            self._condition.notify()
            return True

    def get(self, timeout: float | None = None) -> _QueuedRecord | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._size == 0 and not self._closed:
                remaining = (
                    None if deadline is None else deadline - time.monotonic()
                )
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining)
            if self._size == 0:
                return None
            for tier in self._TIERS:
                if self._queues[tier]:
                    self._size -= 1
                    return self._queues[tier].popleft()
            return None

    def get_nowait(self) -> _QueuedRecord | None:
        with self._condition:
            if self._size == 0:
                return None
            for tier in self._TIERS:
                if self._queues[tier]:
                    self._size -= 1
                    return self._queues[tier].popleft()
            return None

    def task_done(self, count: int = 1) -> None:
        with self._condition:
            self._unfinished = max(0, self._unfinished - count)
            if self._unfinished == 0:
                self._condition.notify_all()

    def wait_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._unfinished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def stats(self) -> dict[str, Any]:
        with self._condition:
            return {
                "depth": self._size,
                "unfinished": self._unfinished,
                "capacity": self.capacity,
                "dropped_by_tier": dict(self._dropped),
                "dropped": sum(self._dropped.values()),
            }


class EmergencyJsonlWriter:
    """仅用于观测存储失效或关键记录无法入队时的最后防线。"""

    def __init__(
        self,
        path: str | Path = DEFAULT_EMERGENCY_PATH,
        *,
        max_bytes: int = 20 * 1024 * 1024,
        backup_count: int = 5,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.Lock()
        self.write_errors = 0

    def write(self, record: dict[str, Any]) -> bool:
        safe = redact_value({"timestamp": utc_now(), **record})
        encoded = (
            json.dumps(
                safe,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            + "\n"
        ).encode("utf-8")
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if (
                    self.path.exists()
                    and self.path.stat().st_size + len(encoded) > self.max_bytes
                ):
                    self._rotate()
                with self.path.open("ab") as stream:
                    stream.write(encoded)
            return True
        except OSError:
            self.write_errors += 1
            return False

    def _rotate(self) -> None:
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(
                    self.path.with_name(f"{self.path.name}.{index + 1}")
                )
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))


class SQLiteObservationStore:
    """单写者 SQLite 存储。

    业务线程只做有界内存入队。所有 SQLite 写操作均由后台单线程串行化；
    查询则使用短生命周期只读连接。
    """

    def __init__(
        self,
        database_path: str | Path = DEFAULT_DB_PATH,
        *,
        queue_capacity: int = 10_000,
        emergency_path: str | Path | None = None,
        busy_timeout_ms: int = 5_000,
        max_database_bytes: int = 1024 * 1024 * 1024,
        target_database_bytes: int = 800 * 1024 * 1024,
    ) -> None:
        self.database_path = Path(database_path)
        self.busy_timeout_ms = busy_timeout_ms
        self.max_database_bytes = max_database_bytes
        self.target_database_bytes = target_database_bytes
        self.emergency = EmergencyJsonlWriter(
            emergency_path
            if emergency_path is not None
            else self.database_path.parent / "logs" / "emergency.jsonl"
        )
        self._queue = _TieredQueue(queue_capacity)
        self._stop = threading.Event()
        self._health_lock = threading.Lock()
        self._degraded = False
        self._last_successful_write_at: str | None = None
        self._last_error: str | None = None
        self._write_errors = 0
        self._records_written = 0
        self._initialized = False
        try:
            self._initialize()
            self._initialized = True
        except (OSError, sqlite3.Error) as exc:
            self._mark_error(exc)
            self.emergency.write(
                {
                    "kind": "observability.initialization_error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
        self._writer = threading.Thread(
            target=self._writer_loop,
            name="swarm-observability-sqlite-writer",
            daemon=True,
        )
        self._writer.start()

    # -- public write API -------------------------------------------------

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        priority: str = "normal",
    ) -> bool:
        record = _QueuedRecord(
            kind=kind,
            payload=redact_value(payload),
            tier=priority if priority in {"critical", "normal", "debug"} else "normal",
        )
        accepted = self._queue.put(record)
        if not accepted and record.tier == "critical":
            self.emergency.write(
                {
                    "kind": "critical_queue_overflow",
                    "record_kind": kind,
                    "record": payload,
                }
            )
        return accepted

    def flush(self, timeout_s: float = 2.0) -> bool:
        return self._queue.wait_empty(timeout_s)

    def close(self, timeout_s: float = 5.0) -> bool:
        flushed = self.flush(timeout_s)
        self._stop.set()
        self._queue.close()
        self._writer.join(max(0.0, timeout_s))
        return flushed and not self._writer.is_alive()

    # -- public query API -------------------------------------------------

    def list_runs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        return self._query(sql, params)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM runs WHERE run_id = ?", [run_id])
        return rows[0] if rows else None

    def query_timeline(
        self,
        run_id: str,
        *,
        since: int = 0,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        return self._query(
            """
            SELECT * FROM events
            WHERE run_id = ? AND sequence > ?
            ORDER BY sequence ASC LIMIT ?
            """,
            [run_id, max(0, since), max(1, min(limit, 50_000))],
        )

    def query_trace(self, run_id: str) -> list[dict[str, Any]]:
        return self._query(
            """
            SELECT * FROM spans WHERE run_id = ?
            ORDER BY start_time ASC, span_id ASC
            """,
            [run_id],
        )

    def query_logs(
        self,
        run_id: str,
        *,
        level: str | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM logs WHERE run_id = ?"
        params: list[Any] = [run_id]
        if level:
            sql += " AND level = ?"
            params.append(level.upper())
        sql += " ORDER BY sequence ASC LIMIT ?"
        params.append(max(1, min(limit, 50_000)))
        return self._query(sql, params)

    def query_errors(self, run_id: str) -> list[dict[str, Any]]:
        return self._query(
            """
            SELECT 'span' AS source, span_id AS source_id, name,
                   end_time AS timestamp, status_message AS message,
                   attributes_json
            FROM spans
            WHERE run_id = ? AND status_code = 'ERROR'
            UNION ALL
            SELECT 'log' AS source, log_id AS source_id, operation AS name,
                   timestamp, message, attributes_json
            FROM logs
            WHERE run_id = ? AND (level IN ('ERROR', 'CRITICAL') OR error_type IS NOT NULL)
            UNION ALL
            SELECT 'event' AS source, event_id AS source_id, name,
                   timestamp, error_message AS message, attributes_json
            FROM events
            WHERE run_id = ? AND (level IN ('ERROR', 'CRITICAL') OR error_type IS NOT NULL)
            ORDER BY timestamp ASC
            """,
            [run_id, run_id, run_id],
        )

    def query_metrics(
        self,
        run_id: str | None = None,
        *,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        if run_id:
            return self._query(
                """
                SELECT * FROM metric_points WHERE run_id = ?
                ORDER BY timestamp ASC LIMIT ?
                """,
                [run_id, max(1, min(limit, 50_000))],
            )
        return self._query(
            "SELECT * FROM metric_points ORDER BY timestamp DESC LIMIT ?",
            [max(1, min(limit, 50_000))],
        )

    def get_payload(self, payload_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM payloads WHERE payload_id = ?", [payload_id]
        )
        return rows[0] if rows else None

    def query_trace_assets(
        self,
        *,
        run_id: str | None = None,
        quality_status: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if quality_status:
            clauses.append("quality_status = ?")
            params.append(quality_status)
        sql = "SELECT * FROM trace_assets"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 10_000)))
        return self._query(sql, params)

    def get_events_since(
        self,
        sequence: int,
        *,
        run_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if run_id:
            return self._query(
                """
                SELECT * FROM events
                WHERE sequence > ? AND run_id = ?
                ORDER BY sequence ASC LIMIT ?
                """,
                [max(0, sequence), run_id, max(1, min(limit, 10_000))],
            )
        return self._query(
            """
            SELECT * FROM events WHERE sequence > ?
            ORDER BY sequence ASC LIMIT ?
            """,
            [max(0, sequence), max(1, min(limit, 10_000))],
        )

    def health(self) -> dict[str, Any]:
        queue_stats = self._queue.stats()
        with self._health_lock:
            return {
                "status": "degraded" if self._degraded else "ok",
                "degraded": self._degraded,
                "database_path": str(self.database_path),
                "queue_depth": queue_stats["depth"],
                "queue_unfinished": queue_stats["unfinished"],
                "queue_capacity": queue_stats["capacity"],
                "dropped": queue_stats["dropped"],
                "dropped_by_tier": queue_stats["dropped_by_tier"],
                "write_errors": self._write_errors,
                "emergency_write_errors": self.emergency.write_errors,
                "last_error": self._last_error,
                "last_successful_write_at": self._last_successful_write_at,
                "records_written": self._records_written,
            }

    # -- initialization / retention --------------------------------------

    def _connect(self, *, ensure_wal: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=max(0.1, self.busy_timeout_ms / 1000),
        )
        connection.row_factory = sqlite3.Row
        if ensure_wal:
            connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect(ensure_wal=True)) as connection:
            with connection:
                connection.executescript(
                    """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    trace_id TEXT,
                    task_id TEXT,
                    task_revision INTEGER,
                    coordination_epoch INTEGER,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    duration_ms REAL,
                    status TEXT NOT NULL,
                    capture_mode TEXT NOT NULL,
                    attributes_json TEXT,
                    error_type TEXT,
                    error_message TEXT
                );

                CREATE TABLE IF NOT EXISTS spans (
                    span_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    parent_span_id TEXT,
                    run_id TEXT,
                    task_id TEXT,
                    name TEXT NOT NULL,
                    kind TEXT,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    status_code TEXT NOT NULL,
                    status_message TEXT,
                    attributes_json TEXT,
                    events_json TEXT,
                    links_json TEXT,
                    input_payload_id TEXT,
                    output_payload_id TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    run_id TEXT,
                    trace_id TEXT,
                    span_id TEXT,
                    parent_span_id TEXT,
                    task_id TEXT,
                    task_revision INTEGER,
                    coordination_epoch INTEGER,
                    agent_id TEXT,
                    device_id TEXT,
                    intent_id TEXT,
                    call_id TEXT,
                    event_offset INTEGER,
                    category TEXT NOT NULL,
                    name TEXT NOT NULL,
                    level TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    attributes_json TEXT,
                    payload_id TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS logs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    log_id TEXT NOT NULL UNIQUE,
                    run_id TEXT,
                    trace_id TEXT,
                    span_id TEXT,
                    parent_span_id TEXT,
                    task_id TEXT,
                    agent_id TEXT,
                    device_id TEXT,
                    intent_id TEXT,
                    event_id TEXT,
                    call_id TEXT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    module TEXT NOT NULL,
                    operation TEXT,
                    message TEXT NOT NULL,
                    error_type TEXT,
                    stack_trace TEXT,
                    attributes_json TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS metric_points (
                    point_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    timestamp TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    instrument_type TEXT NOT NULL,
                    value REAL NOT NULL,
                    unit TEXT,
                    attributes_json TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS payloads (
                    payload_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    trace_id TEXT,
                    span_id TEXT,
                    event_id TEXT,
                    direction TEXT NOT NULL,
                    capture_mode TEXT NOT NULL,
                    content_type TEXT,
                    schema_json TEXT,
                    byte_size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    summary TEXT,
                    redacted_content_json TEXT,
                    is_truncated INTEGER NOT NULL DEFAULT 0,
                    content_available INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS trace_assets (
                    asset_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    trace_id TEXT,
                    task_id TEXT,
                    quality_status TEXT NOT NULL,
                    quality_json TEXT,
                    episode_json TEXT,
                    source_span_ids_json TEXT,
                    source_event_ids_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_runs_started
                    ON runs(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_spans_run_start
                    ON spans(run_id, start_time);
                CREATE INDEX IF NOT EXISTS idx_spans_trace
                    ON spans(trace_id, span_id);
                CREATE INDEX IF NOT EXISTS idx_events_run_sequence
                    ON events(run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_events_task
                    ON events(task_id, event_offset);
                CREATE INDEX IF NOT EXISTS idx_logs_run_sequence
                    ON logs(run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_metrics_run_name
                    ON metric_points(run_id, metric_name, timestamp);
                CREATE INDEX IF NOT EXISTS idx_payloads_run
                    ON payloads(run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_assets_status
                    ON trace_assets(quality_status, created_at);
                    """
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                    VALUES (?, ?)
                    """,
                    (SCHEMA_VERSION, utc_now()),
                )
        self.enforce_retention()

    def enforce_retention(self) -> None:
        if not self.database_path.exists():
            return
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        """
                        UPDATE payloads
                        SET redacted_content_json = NULL,
                            content_available = 0,
                            capture_mode = 'metadata'
                        WHERE content_available = 1
                          AND datetime(created_at) < datetime('now', '-7 days')
                        """
                    )
                    connection.execute(
                        """
                        DELETE FROM trace_assets
                        WHERE datetime(created_at) < datetime('now', '-180 days')
                        """
                    )
                    connection.execute(
                        """
                        DELETE FROM runs
                        WHERE ended_at IS NOT NULL
                          AND datetime(ended_at) < datetime('now', '-30 days')
                        """
                    )
            if self._database_size() > self.max_database_bytes:
                self._trim_to_target_size()
        except (OSError, sqlite3.Error) as exc:
            self._mark_error(exc)

    def _database_size(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.database_path) + suffix)
            if path.exists():
                total += path.stat().st_size
        return total

    def _trim_to_target_size(self) -> None:
        deleted_any = False
        with closing(self._connect()) as connection:
            while self._logical_database_size(connection) > self.target_database_bytes:
                rows = connection.execute(
                    """
                    SELECT run_id FROM runs
                    WHERE ended_at IS NOT NULL
                    ORDER BY ended_at ASC LIMIT 50
                    """
                ).fetchall()
                if not rows:
                    return
                with connection:
                    connection.executemany(
                        "DELETE FROM runs WHERE run_id = ?",
                        [(row["run_id"],) for row in rows],
                    )
                deleted_any = True
            if deleted_any:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")

    @staticmethod
    def _logical_database_size(connection: sqlite3.Connection) -> int:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(
            connection.execute("PRAGMA freelist_count").fetchone()[0]
        )
        return max(0, page_count - free_pages) * page_size

    # -- writer -----------------------------------------------------------

    def _writer_loop(self) -> None:
        connection: sqlite3.Connection | None = None
        while not self._stop.is_set() or self._queue.stats()["depth"] > 0:
            first = self._queue.get(timeout=0.2)
            if first is None:
                continue
            batch = [first]
            while len(batch) < 100:
                item = self._queue.get_nowait()
                if item is None:
                    break
                batch.append(item)
            written = False
            for delay in (0.0, 0.05, 0.2, 0.5):
                if delay:
                    time.sleep(delay)
                try:
                    if not self._initialized:
                        self._initialize()
                        self._initialized = True
                    if connection is None:
                        connection = self._connect()
                    connection.execute("BEGIN")
                    for record in batch:
                        self._write_record(connection, record)
                    connection.commit()
                    written = True
                    with self._health_lock:
                        self._last_successful_write_at = utc_now()
                        self._records_written += len(batch)
                        self._degraded = False
                        self._last_error = None
                    break
                except (OSError, sqlite3.Error) as exc:
                    if connection is not None:
                        try:
                            connection.rollback()
                            connection.close()
                        except sqlite3.Error:
                            pass
                        connection = None
                    self._mark_error(exc)
            if not written:
                for record in batch:
                    self.emergency.write(
                        {
                            "kind": "sqlite_write_failure",
                            "record_kind": record.kind,
                            "record": record.payload,
                        }
                    )
            self._queue.task_done(len(batch))
            if written and self._records_written % 1000 < len(batch):
                self.enforce_retention()
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def _write_record(
        self, connection: sqlite3.Connection, record: _QueuedRecord
    ) -> None:
        payload = record.payload
        if record.kind == "run_start":
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, trace_id, task_id, task_revision,
                    coordination_epoch, started_at, status, capture_mode,
                    attributes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    trace_id = COALESCE(excluded.trace_id, runs.trace_id),
                    task_id = COALESCE(excluded.task_id, runs.task_id),
                    task_revision = COALESCE(excluded.task_revision, runs.task_revision),
                    coordination_epoch = COALESCE(
                        excluded.coordination_epoch, runs.coordination_epoch
                    ),
                    status = excluded.status,
                    capture_mode = excluded.capture_mode,
                    attributes_json = excluded.attributes_json
                """,
                (
                    payload["run_id"],
                    payload.get("trace_id"),
                    payload.get("task_id"),
                    payload.get("task_revision"),
                    payload.get("coordination_epoch"),
                    payload.get("started_at", utc_now()),
                    payload.get("status", "running"),
                    payload.get("capture_mode", "metadata"),
                    _json(payload.get("attributes", {})),
                ),
            )
        elif record.kind == "run_trace":
            connection.execute(
                "UPDATE runs SET trace_id = ? WHERE run_id = ?",
                (payload.get("trace_id"), payload["run_id"]),
            )
        elif record.kind == "run_update":
            connection.execute(
                """
                UPDATE runs SET
                    task_id = COALESCE(?, task_id),
                    task_revision = COALESCE(?, task_revision),
                    coordination_epoch = COALESCE(?, coordination_epoch),
                    attributes_json = COALESCE(?, attributes_json)
                WHERE run_id = ?
                """,
                (
                    payload.get("task_id"),
                    payload.get("task_revision"),
                    payload.get("coordination_epoch"),
                    (
                        _json(payload["attributes"])
                        if payload.get("attributes") is not None
                        else None
                    ),
                    payload["run_id"],
                ),
            )
        elif record.kind == "run_end":
            connection.execute(
                """
                UPDATE runs SET ended_at = ?, duration_ms = ?, status = ?,
                    error_type = ?, error_message = ?,
                    attributes_json = ?
                WHERE run_id = ?
                """,
                (
                    payload.get("ended_at", utc_now()),
                    payload.get("duration_ms"),
                    payload.get("status", "ok"),
                    payload.get("error_type"),
                    payload.get("error_message"),
                    _json(payload.get("attributes", {})),
                    payload["run_id"],
                ),
            )
        elif record.kind == "span":
            connection.execute(
                """
                INSERT OR REPLACE INTO spans(
                    span_id, trace_id, parent_span_id, run_id, task_id, name,
                    kind, start_time, end_time, duration_ms, status_code,
                    status_message, attributes_json, events_json, links_json,
                    input_payload_id, output_payload_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["span_id"],
                    payload["trace_id"],
                    payload.get("parent_span_id"),
                    payload.get("run_id"),
                    payload.get("task_id"),
                    payload["name"],
                    payload.get("kind", "INTERNAL"),
                    payload["start_time"],
                    payload["end_time"],
                    payload.get("duration_ms", 0.0),
                    payload.get("status_code", "UNSET"),
                    payload.get("status_message"),
                    _json(payload.get("attributes", {})),
                    _json(payload.get("events", [])),
                    _json(payload.get("links", [])),
                    payload.get("input_payload_id"),
                    payload.get("output_payload_id"),
                    payload.get("created_at", utc_now()),
                ),
            )
        elif record.kind == "event":
            connection.execute(
                """
                INSERT OR IGNORE INTO events(
                    event_id, run_id, trace_id, span_id, parent_span_id,
                    task_id, task_revision, coordination_epoch, agent_id,
                    device_id, intent_id, call_id, event_offset, category,
                    name, level, timestamp, attributes_json, payload_id,
                    error_type, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["event_id"],
                    payload.get("run_id"),
                    payload.get("trace_id"),
                    payload.get("span_id"),
                    payload.get("parent_span_id"),
                    payload.get("task_id"),
                    payload.get("task_revision"),
                    payload.get("coordination_epoch"),
                    payload.get("agent_id"),
                    payload.get("device_id"),
                    payload.get("intent_id"),
                    payload.get("call_id"),
                    payload.get("offset"),
                    payload.get("category", "observation"),
                    payload["name"],
                    payload.get("level", "INFO"),
                    payload.get("timestamp", utc_now()),
                    _json(payload.get("attributes", {})),
                    payload.get("payload_id"),
                    payload.get("error_type"),
                    payload.get("error_message"),
                ),
            )
        elif record.kind == "log":
            connection.execute(
                """
                INSERT OR IGNORE INTO logs(
                    log_id, run_id, trace_id, span_id, parent_span_id,
                    task_id, agent_id, device_id, intent_id, event_id,
                    call_id, timestamp, level, module, operation, message,
                    error_type, stack_trace, attributes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["log_id"],
                    payload.get("run_id"),
                    payload.get("trace_id"),
                    payload.get("span_id"),
                    payload.get("parent_span_id"),
                    payload.get("task_id"),
                    payload.get("agent_id"),
                    payload.get("device_id"),
                    payload.get("intent_id"),
                    payload.get("event_id"),
                    payload.get("call_id"),
                    payload.get("timestamp", utc_now()),
                    payload.get("level", "INFO"),
                    payload.get("module", ""),
                    payload.get("operation"),
                    payload.get("message", ""),
                    payload.get("error_type"),
                    payload.get("stack_trace"),
                    _json(payload.get("attributes", {})),
                ),
            )
        elif record.kind == "metric":
            connection.execute(
                """
                INSERT INTO metric_points(
                    run_id, timestamp, metric_name, instrument_type,
                    value, unit, attributes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.get("run_id"),
                    payload.get("timestamp", utc_now()),
                    payload["metric_name"],
                    payload["instrument_type"],
                    payload.get("value", 0.0),
                    payload.get("unit"),
                    _json(payload.get("attributes", {})),
                ),
            )
        elif record.kind == "payload":
            connection.execute(
                """
                INSERT OR REPLACE INTO payloads(
                    payload_id, run_id, trace_id, span_id, event_id,
                    direction, capture_mode, content_type, schema_json,
                    byte_size, sha256, summary, redacted_content_json,
                    is_truncated, content_available, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["payload_id"],
                    payload.get("run_id"),
                    payload.get("trace_id"),
                    payload.get("span_id"),
                    payload.get("event_id"),
                    payload.get("direction", "unknown"),
                    payload.get("capture_mode", "metadata"),
                    payload.get("content_type"),
                    _json(payload.get("schema_json", {})),
                    payload.get("byte_size", 0),
                    payload.get("sha256", ""),
                    payload.get("summary"),
                    payload.get("redacted_content_json"),
                    payload.get("is_truncated", 0),
                    payload.get("content_available", 0),
                    payload.get("created_at", utc_now()),
                    payload.get("expires_at"),
                ),
            )
        elif record.kind == "trace_asset":
            connection.execute(
                """
                INSERT OR REPLACE INTO trace_assets(
                    asset_id, run_id, trace_id, task_id, quality_status,
                    quality_json, episode_json, source_span_ids_json,
                    source_event_ids_json, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["asset_id"],
                    payload.get("run_id"),
                    payload.get("trace_id"),
                    payload.get("task_id"),
                    payload.get("quality_status", "raw"),
                    _json(payload.get("quality", {})),
                    _json(payload.get("episode", {})),
                    _json(payload.get("source_span_ids", [])),
                    _json(payload.get("source_event_ids", [])),
                    payload.get("created_at", utc_now()),
                    payload.get("updated_at", utc_now()),
                    payload.get("expires_at"),
                ),
            )

    # -- helpers ----------------------------------------------------------

    def _query(self, sql: str, params: Iterable[Any]) -> list[dict[str, Any]]:
        if not self._initialized:
            return []
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(sql, list(params)).fetchall()
            return [self._decode_row(dict(row)) for row in rows]
        except (OSError, sqlite3.Error) as exc:
            self._mark_error(exc)
            return []

    @staticmethod
    def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
        for key in tuple(row):
            if key.endswith("_json"):
                decoded_key = key[:-5]
                row[decoded_key] = _loads(row.pop(key))
        for key in ("is_truncated", "content_available"):
            if key in row:
                row[key] = bool(row[key])
        return row

    def _mark_error(self, exc: BaseException) -> None:
        with self._health_lock:
            self._degraded = True
            self._write_errors += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
