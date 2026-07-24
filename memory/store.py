"""SQLite WAL implementation of the V2 memory port."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .models import (
    CandidateStatus,
    CandidateType,
    Provenance,
    TraceAssetStatus,
    canonical_json,
    content_digest,
    fts_query,
    redact_value,
    searchable_text,
)


SCHEMA_VERSION = 1
_FACT_EVIDENCE_TYPES = {
    "structured_task",
    "authoritative_evidence",
    "confirmed_receipt",
    "human_confirmation",
}


def default_memory_path() -> Path:
    configured = os.environ.get("SWARM_BRAIN_MEMORY_DB")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent / "runtime_data" / "memory.sqlite3"


class NoOpMemory:
    """Drop-in MemoryPort used when persistence is not injected."""

    def store_private(self, agent_id: str, kind: str, item: dict) -> None:
        return None

    def retrieve_private(
        self,
        agent_id: str,
        kind: str,
        query: Optional[dict] = None,
    ) -> list[dict[str, Any]]:
        return []

    def read_facts(self, topic: str) -> list[dict[str, Any]]:
        return []

    def write_fact(self, topic: str, fact: dict, source: str) -> None:
        return None

    def retrieve_context(
        self,
        *,
        agent_id: Optional[str],
        query: str,
        private_limit: int = 3,
        fact_limit: int = 5,
        max_chars: int = 4000,
        shared_only: bool = False,
    ) -> dict[str, Any]:
        return {
            "private_episodes": [],
            "shared_facts": [],
            "adopted_memory_ids": [],
            "context": "",
            "character_count": 0,
            "max_chars": max_chars,
            "shared_only": shared_only,
        }

    def retrieve_shared_context(
        self,
        query: str,
        *,
        fact_limit: int = 5,
        max_chars: int = 4000,
    ) -> dict[str, Any]:
        return self.retrieve_context(
            agent_id=None,
            query=query,
            fact_limit=fact_limit,
            max_chars=max_chars,
            shared_only=True,
        )

    def clear_working_memory(
        self,
        *,
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> int:
        return 0

    def close(self) -> None:
        return None


class SQLiteMemoryStore:
    """Thread-safe persistent memory with review-gated shared knowledge.

    The four legacy ``MemoryPort`` methods are implemented directly:
    ``store_private``, ``retrieve_private``, ``read_facts`` and ``write_fact``.
    Richer methods return record metadata and expose candidate decisions.
    """

    def __init__(self, path: Optional[str | os.PathLike[str]] = None) -> None:
        self.path = Path(path) if path is not None else default_memory_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            str(self.path),
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    def _configure(self) -> None:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("PRAGMA busy_timeout = 5000")
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute("PRAGMA journal_mode = WAL")

    def _migrate(self) -> None:
        migration = f"""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS private_memories (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('working', 'episodic')),
            task_id TEXT,
            content_json TEXT NOT NULL,
            searchable_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            source_trace_id TEXT NOT NULL,
            source_span_id TEXT NOT NULL,
            source_event_ids_json TEXT NOT NULL,
            source_type TEXT NOT NULL,
            confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL,
            supersedes_id TEXT REFERENCES private_memories(id),
            retired_at REAL,
            UNIQUE(agent_id, kind, content_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_private_scope
            ON private_memories(agent_id, kind, retired_at, expires_at, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_private_task
            ON private_memories(agent_id, task_id, kind);

        CREATE TABLE IF NOT EXISTS shared_facts (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            fact_json TEXT NOT NULL,
            searchable_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            source_trace_id TEXT NOT NULL,
            source_span_id TEXT NOT NULL,
            source_event_ids_json TEXT NOT NULL,
            source_type TEXT NOT NULL,
            confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL,
            supersedes_id TEXT REFERENCES shared_facts(id),
            retired_at REAL,
            approved_at REAL NOT NULL,
            approved_by TEXT NOT NULL,
            UNIQUE(topic, content_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_fact_topic
            ON shared_facts(topic, retired_at, expires_at, confidence DESC, created_at DESC);

        CREATE TABLE IF NOT EXISTS memory_candidates (
            id TEXT PRIMARY KEY,
            candidate_type TEXT NOT NULL CHECK (candidate_type IN ('fact', 'skill')),
            topic TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            searchable_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending_review', 'approved', 'rejected', 'archived')
            ),
            source_trace_id TEXT NOT NULL,
            source_span_id TEXT NOT NULL,
            source_event_ids_json TEXT NOT NULL,
            source_type TEXT NOT NULL,
            confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            share_eligible INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL,
            supersedes_id TEXT,
            decided_at REAL,
            decided_by TEXT,
            decision_reason TEXT,
            promoted_memory_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_review
            ON memory_candidates(status, candidate_type, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_candidate_dedupe
            ON memory_candidates(candidate_type, topic, content_hash, status);

        CREATE TABLE IF NOT EXISTS trace_asset_consolidations (
            asset_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            quality_status TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            private_memory_ids_json TEXT NOT NULL,
            candidate_ids_json TEXT NOT NULL,
            consolidated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_consolidation_trace
            ON trace_asset_consolidations(trace_id, task_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS private_memory_fts USING fts5(
            memory_id UNINDEXED,
            agent_id UNINDEXED,
            kind UNINDEXED,
            searchable_text,
            tokenize = 'unicode61 remove_diacritics 2'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS shared_fact_fts USING fts5(
            fact_id UNINDEXED,
            topic,
            searchable_text,
            tokenize = 'unicode61 remove_diacritics 2'
        );

        INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
        VALUES ({SCHEMA_VERSION}, 'initial_memory_schema', CAST(strftime('%s', 'now') AS REAL));
        """
        with self._lock:
            try:
                # sqlite3.executescript commits a pending transaction first, so
                # the transaction markers must live inside the script itself.
                self._connection.executescript(
                    f"BEGIN IMMEDIATE;\n{migration}\nCOMMIT;"
                )
            except sqlite3.OperationalError as exc:
                self._connection.rollback()
                if "fts5" in str(exc).lower():
                    raise RuntimeError("SQLite was built without required FTS5 support") from exc
                raise

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> "SQLiteMemoryStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # -- Legacy MemoryPort -------------------------------------------------

    def store_private(self, agent_id: str, kind: str, item: dict) -> None:
        self.put_private(agent_id, kind, item)

    def retrieve_private(
        self,
        agent_id: str,
        kind: str,
        query: Optional[dict] = None,
    ) -> list[dict[str, Any]]:
        query = dict(query or {})
        return self.search_private(
            agent_id=agent_id,
            kind=kind,
            text=str(query.get("text") or query.get("query") or ""),
            task_id=query.get("task_id"),
            limit=int(query.get("limit", 20)),
            min_confidence=float(query.get("min_confidence", 0.0)),
            include_expired=bool(query.get("include_expired", False)),
            include_retired=bool(query.get("include_retired", False)),
        )

    def read_facts(self, topic: str) -> list[dict[str, Any]]:
        exact = self.search_facts(topic=topic, limit=20)
        if exact or not topic:
            return exact
        return self.search_facts(text=topic, limit=20)

    def write_fact(self, topic: str, fact: dict, source: str) -> None:
        """Submit a fact for review; never bypass the shared-memory gate."""

        source_type = source if source in _FACT_EVIDENCE_TYPES else "legacy"
        self.submit_fact_candidate(
            topic=topic,
            fact=fact,
            provenance=Provenance.from_value(
                fact.get("provenance") if isinstance(fact, dict) else None,
                default_type=source_type,
            ),
            confidence=float(fact.get("confidence", 0.5)) if isinstance(fact, dict) else 0.5,
            source_type=source_type,
        )

    # -- Private memory ----------------------------------------------------

    def put_private(
        self,
        agent_id: str,
        kind: str,
        item: Mapping[str, Any],
        *,
        provenance: Optional[Provenance | Mapping[str, Any]] = None,
        confidence: Optional[float] = None,
        expires_at: Optional[float] = None,
        supersedes_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if not agent_id:
            raise ValueError("agent_id is required")
        if kind not in {"working", "episodic"}:
            raise ValueError("kind must be 'working' or 'episodic'")
        raw = dict(item)
        content = redact_value(raw.get("content", raw))
        task_id = task_id or raw.get("task_id")
        confidence = self._confidence(
            confidence if confidence is not None else raw.get("confidence", 0.5)
        )
        expires_at = expires_at if expires_at is not None else raw.get("expires_at")
        supersedes_id = supersedes_id or raw.get("supersedes_id")
        provenance_value = provenance or raw.get("provenance") or raw
        provenance_obj = (
            provenance_value
            if isinstance(provenance_value, Provenance)
            else Provenance.from_value(provenance_value, default_type="private_write")
        )
        digest = content_digest(
            {
                "agent_id": agent_id,
                "kind": kind,
                "task_id": task_id,
                "content": content,
                "trace_id": provenance_obj.source_trace_id,
            }
        )
        memory_id = str(raw.get("id") or f"mem:{digest}")
        now = time.time()
        with self._transaction() as connection:
            if supersedes_id:
                previous = connection.execute(
                    "SELECT agent_id FROM private_memories WHERE id = ?",
                    (supersedes_id,),
                ).fetchone()
                if previous is None:
                    raise ValueError("superseded private memory does not exist")
                if previous["agent_id"] != agent_id:
                    raise ValueError("cannot supersede another agent's private memory")
                connection.execute(
                    "UPDATE private_memories SET retired_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, supersedes_id),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO private_memories(
                    id, agent_id, kind, task_id, content_json, searchable_text,
                    content_hash, source_trace_id, source_span_id,
                    source_event_ids_json, source_type, confidence, created_at,
                    updated_at, expires_at, supersedes_id, retired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    memory_id,
                    agent_id,
                    kind,
                    task_id,
                    canonical_json(content),
                    searchable_text(content),
                    digest,
                    provenance_obj.source_trace_id,
                    provenance_obj.source_span_id,
                    canonical_json(provenance_obj.source_event_ids),
                    provenance_obj.source_type,
                    confidence,
                    now,
                    now,
                    expires_at,
                    supersedes_id,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM private_memories
                WHERE agent_id = ? AND kind = ? AND content_hash = ?
                """,
                (agent_id, kind, digest),
            ).fetchone()
            assert row is not None
            connection.execute(
                "DELETE FROM private_memory_fts WHERE memory_id = ?",
                (row["id"],),
            )
            connection.execute(
                """
                INSERT INTO private_memory_fts(memory_id, agent_id, kind, searchable_text)
                VALUES (?, ?, ?, ?)
                """,
                (row["id"], agent_id, kind, row["searchable_text"]),
            )
        return self._private_record(row)

    def search_private(
        self,
        *,
        agent_id: str,
        kind: str = "episodic",
        text: str = "",
        task_id: Optional[str] = None,
        limit: int = 3,
        min_confidence: float = 0.0,
        include_expired: bool = False,
        include_retired: bool = False,
    ) -> list[dict[str, Any]]:
        if not agent_id:
            raise ValueError("agent_id is required")
        if kind not in {"working", "episodic"}:
            raise ValueError("invalid private memory kind")
        limit = max(1, min(int(limit), 100))
        conditions = ["p.agent_id = ?", "p.kind = ?", "p.confidence >= ?"]
        params: list[Any] = [agent_id, kind, self._confidence(min_confidence)]
        if task_id is not None:
            conditions.append("p.task_id = ?")
            params.append(task_id)
        now = time.time()
        if not include_expired:
            conditions.append("(p.expires_at IS NULL OR p.expires_at > ?)")
            params.append(now)
        if not include_retired:
            conditions.append("p.retired_at IS NULL")
        expression = fts_query(text)
        if expression:
            sql = f"""
                SELECT p.*, bm25(private_memory_fts) AS search_rank
                FROM private_memory_fts
                JOIN private_memories p ON p.id = private_memory_fts.memory_id
                WHERE private_memory_fts MATCH ? AND {' AND '.join(conditions)}
                ORDER BY search_rank ASC, p.confidence DESC, p.created_at DESC
                LIMIT ?
            """
            params = [expression, *params, limit]
        else:
            sql = f"""
                SELECT p.*, 0.0 AS search_rank
                FROM private_memories p
                WHERE {' AND '.join(conditions)}
                ORDER BY p.confidence DESC, p.created_at DESC
                LIMIT ?
            """
            params.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [self._private_record(row) for row in rows]

    def clear_working_memory(
        self,
        *,
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> int:
        """Remove task-scoped working memory at terminal convergence."""

        conditions = ["kind = 'working'"]
        params: list[Any] = []
        if agent_id is not None:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if task_id is not None:
            conditions.append("task_id = ?")
            params.append(task_id)
        if agent_id is None and task_id is None:
            raise ValueError("agent_id or task_id is required")
        with self._transaction() as connection:
            ids = [
                row["id"]
                for row in connection.execute(
                    f"SELECT id FROM private_memories WHERE {' AND '.join(conditions)}",
                    params,
                ).fetchall()
            ]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"DELETE FROM private_memory_fts WHERE memory_id IN ({placeholders})",
                    ids,
                )
                connection.execute(
                    f"DELETE FROM private_memories WHERE id IN ({placeholders})",
                    ids,
                )
        return len(ids)

    # -- Shared facts and review candidates -------------------------------

    def submit_fact_candidate(
        self,
        *,
        topic: str,
        fact: Mapping[str, Any],
        provenance: Provenance | Mapping[str, Any],
        confidence: float,
        source_type: Optional[str] = None,
        expires_at: Optional[float] = None,
        supersedes_id: Optional[str] = None,
    ) -> dict[str, Any]:
        provenance_obj = (
            provenance
            if isinstance(provenance, Provenance)
            else Provenance.from_value(provenance, default_type=source_type or "trace_asset")
        )
        resolved_type = source_type or provenance_obj.source_type
        return self.add_candidate(
            candidate_type=CandidateType.FACT,
            topic=topic,
            payload={"fact": dict(fact)},
            provenance=Provenance(
                provenance_obj.source_trace_id,
                provenance_obj.source_span_id,
                provenance_obj.source_event_ids,
                resolved_type,
            ),
            confidence=confidence,
            expires_at=expires_at,
            supersedes_id=supersedes_id,
            share_eligible=resolved_type in _FACT_EVIDENCE_TYPES,
        )

    def submit_skill_candidate(
        self,
        *,
        topic: str,
        skill: Mapping[str, Any],
        provenance: Provenance | Mapping[str, Any],
        confidence: float,
        expires_at: Optional[float] = None,
        supersedes_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return self.add_candidate(
            candidate_type=CandidateType.SKILL,
            topic=topic,
            payload={"skill": dict(skill)},
            provenance=provenance,
            confidence=confidence,
            expires_at=expires_at,
            supersedes_id=supersedes_id,
            share_eligible=False,
        )

    def add_candidate(
        self,
        *,
        candidate_type: CandidateType | str,
        topic: str,
        payload: Mapping[str, Any],
        provenance: Provenance | Mapping[str, Any],
        confidence: float,
        expires_at: Optional[float] = None,
        supersedes_id: Optional[str] = None,
        share_eligible: bool = False,
    ) -> dict[str, Any]:
        resolved_type = CandidateType(candidate_type).value
        if not topic.strip():
            raise ValueError("candidate topic is required")
        provenance_obj = (
            provenance
            if isinstance(provenance, Provenance)
            else Provenance.from_value(provenance, default_type="trace_asset")
        )
        safe_payload = redact_value(dict(payload))
        digest = content_digest({"type": resolved_type, "topic": topic, "payload": safe_payload})
        now = time.time()
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM memory_candidates
                WHERE candidate_type = ? AND topic = ? AND content_hash = ?
                  AND status = 'pending_review'
                ORDER BY created_at DESC LIMIT 1
                """,
                (resolved_type, topic, digest),
            ).fetchone()
            if existing is not None:
                return self._candidate_record(existing)
            candidate_id = f"candidate:{uuid.uuid4()}"
            connection.execute(
                """
                INSERT INTO memory_candidates(
                    id, candidate_type, topic, payload_json, searchable_text,
                    content_hash, status, source_trace_id, source_span_id,
                    source_event_ids_json, source_type, confidence, share_eligible,
                    created_at, updated_at, expires_at, supersedes_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    resolved_type,
                    topic,
                    canonical_json(safe_payload),
                    searchable_text(safe_payload),
                    digest,
                    provenance_obj.source_trace_id,
                    provenance_obj.source_span_id,
                    canonical_json(provenance_obj.source_event_ids),
                    provenance_obj.source_type,
                    self._confidence(confidence),
                    int(bool(share_eligible)),
                    now,
                    now,
                    expires_at,
                    supersedes_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        assert row is not None
        return self._candidate_record(row)

    def list_candidates(
        self,
        *,
        status: CandidateStatus | str = CandidateStatus.PENDING_REVIEW,
        candidate_type: Optional[CandidateType | str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        resolved_status = CandidateStatus(status).value
        conditions = ["status = ?"]
        params: list[Any] = [resolved_status]
        if candidate_type is not None:
            conditions.append("candidate_type = ?")
            params.append(CandidateType(candidate_type).value)
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM memory_candidates
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [self._candidate_record(row) for row in rows]

    def get_candidate(self, candidate_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        return None if row is None else self._candidate_record(row)

    def decide_candidate(
        self,
        candidate_id: str,
        decision: str,
        *,
        decided_by: str,
        reason: str = "",
    ) -> dict[str, Any]:
        decision_value = decision.strip().lower()
        if decision_value in {"approve", "approved"}:
            target_status = CandidateStatus.APPROVED.value
        elif decision_value in {"reject", "rejected"}:
            target_status = CandidateStatus.REJECTED.value
        else:
            raise ValueError("decision must be approve or reject")
        if not decided_by:
            raise ValueError("decided_by is required")
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            if row["status"] != CandidateStatus.PENDING_REVIEW.value:
                if row["status"] == target_status:
                    return self._candidate_record(row)
                raise ValueError(f"candidate is already {row['status']}")
            if (
                target_status == CandidateStatus.APPROVED.value
                and row["expires_at"] is not None
                and row["expires_at"] <= now
            ):
                raise ValueError("expired candidate cannot be approved")
            promoted_id: Optional[str] = None
            if target_status == CandidateStatus.APPROVED.value:
                if row["candidate_type"] == CandidateType.FACT.value:
                    if not bool(row["share_eligible"]):
                        raise ValueError(
                            "fact candidate lacks structured or authoritative evidence"
                        )
                    promoted_id = self._promote_fact(connection, row, decided_by, now)
                # Approved skill candidates intentionally remain candidates.  A
                # SkillGraph owner may consume them, but memory never registers
                # executable skills by itself.
            connection.execute(
                """
                UPDATE memory_candidates
                SET status = ?, updated_at = ?, decided_at = ?, decided_by = ?,
                    decision_reason = ?, promoted_memory_id = ?
                WHERE id = ?
                """,
                (
                    target_status,
                    now,
                    now,
                    decided_by,
                    reason,
                    promoted_id,
                    candidate_id,
                ),
            )
            decided = connection.execute(
                "SELECT * FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        assert decided is not None
        return self._candidate_record(decided)

    def search_facts(
        self,
        *,
        text: str = "",
        topic: Optional[str] = None,
        limit: int = 5,
        min_confidence: float = 0.0,
        include_expired: bool = False,
        include_retired: bool = False,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        conditions = ["sf.confidence >= ?"]
        params: list[Any] = [self._confidence(min_confidence)]
        if topic is not None:
            conditions.append("sf.topic = ?")
            params.append(topic)
        if not include_expired:
            conditions.append("(sf.expires_at IS NULL OR sf.expires_at > ?)")
            params.append(time.time())
        if not include_retired:
            conditions.append("sf.retired_at IS NULL")
        expression = fts_query(text)
        if expression:
            sql = f"""
                SELECT sf.*, bm25(shared_fact_fts) AS search_rank
                FROM shared_fact_fts
                JOIN shared_facts sf ON sf.id = shared_fact_fts.fact_id
                WHERE shared_fact_fts MATCH ? AND {' AND '.join(conditions)}
                ORDER BY search_rank ASC, sf.confidence DESC, sf.created_at DESC
                LIMIT ?
            """
            params = [expression, *params, limit]
        else:
            sql = f"""
                SELECT sf.*, 0.0 AS search_rank
                FROM shared_facts sf
                WHERE {' AND '.join(conditions)}
                ORDER BY sf.confidence DESC, sf.created_at DESC
                LIMIT ?
            """
            params.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [self._fact_record(row) for row in rows]

    def retrieve_context(
        self,
        *,
        agent_id: Optional[str],
        query: str,
        private_limit: int = 3,
        fact_limit: int = 5,
        max_chars: int = 4000,
        shared_only: bool = False,
    ) -> dict[str, Any]:
        """Assemble a bounded retrieval result for BID/local/group planning.

        ``shared_only=True`` is the group-planning boundary: no private row is
        queried or returned.  Skill references remain owned by SkillGraph and
        can be appended by the coordination context assembler.
        """

        if not shared_only and not agent_id:
            raise ValueError("agent_id is required when private memory is enabled")
        budget = max(1, min(int(max_chars), 32_000))
        private = (
            []
            if shared_only
            else self.search_private(
                agent_id=str(agent_id),
                kind="episodic",
                text=query,
                limit=max(1, min(int(private_limit), 20)),
            )
        )
        facts = self.search_facts(
            text=query,
            limit=max(1, min(int(fact_limit), 50)),
        )
        selected_private: list[dict[str, Any]] = []
        selected_facts: list[dict[str, Any]] = []
        text_chunks: list[str] = []
        seen: set[str] = set()
        used = 0
        candidates = [
            ("private", item, item["content"])
            for item in private
        ] + [
            ("fact", item, item["fact"])
            for item in facts
        ]
        for category, record, content in candidates:
            digest = content_digest(content)
            if digest in seen:
                continue
            rendered = canonical_json(content)
            prefix = "[private episode] " if category == "private" else "[shared fact] "
            chunk = prefix + rendered
            separator = 1 if text_chunks else 0
            remaining = budget - used - separator
            if remaining <= len(prefix):
                break
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            text_chunks.append(chunk)
            used += separator + len(chunk)
            seen.add(digest)
            if category == "private":
                selected_private.append(record)
            else:
                selected_facts.append(record)
            if used >= budget:
                break
        return {
            "private_episodes": selected_private,
            "shared_facts": selected_facts,
            "adopted_memory_ids": [
                item["id"] for item in [*selected_private, *selected_facts]
            ],
            "context": "\n".join(text_chunks),
            "character_count": used,
            "max_chars": budget,
            "shared_only": shared_only,
        }

    def retrieve_shared_context(
        self,
        query: str,
        *,
        fact_limit: int = 5,
        max_chars: int = 4000,
    ) -> dict[str, Any]:
        return self.retrieve_context(
            agent_id=None,
            query=query,
            fact_limit=fact_limit,
            max_chars=max_chars,
            shared_only=True,
        )

    def retire_expired(self, *, now: Optional[float] = None) -> dict[str, int]:
        cutoff = time.time() if now is None else float(now)
        with self._transaction() as connection:
            private_count = connection.execute(
                """
                UPDATE private_memories SET retired_at = ?, updated_at = ?
                WHERE retired_at IS NULL AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (cutoff, cutoff, cutoff),
            ).rowcount
            fact_count = connection.execute(
                """
                UPDATE shared_facts SET retired_at = ?, updated_at = ?
                WHERE retired_at IS NULL AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (cutoff, cutoff, cutoff),
            ).rowcount
            candidate_count = connection.execute(
                """
                UPDATE memory_candidates
                SET status = 'archived', updated_at = ?, decided_at = ?,
                    decided_by = 'retention', decision_reason = 'expired'
                WHERE status = 'pending_review'
                  AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (cutoff, cutoff, cutoff),
            ).rowcount
        return {
            "private": private_count,
            "facts": fact_count,
            "candidates": candidate_count,
        }

    # -- TraceAsset consolidation bookkeeping ----------------------------

    def has_consolidation(self, asset_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM trace_asset_consolidations WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        return row is not None

    def get_consolidation(self, asset_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM trace_asset_consolidations WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "asset_id": row["asset_id"],
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "trace_id": row["trace_id"],
            "quality_status": row["quality_status"],
            "reasons": json.loads(row["reasons_json"]),
            "private_memory_ids": json.loads(row["private_memory_ids_json"]),
            "candidate_ids": json.loads(row["candidate_ids_json"]),
            "consolidated_at": row["consolidated_at"],
        }

    def record_consolidation(
        self,
        *,
        asset_id: str,
        run_id: str,
        task_id: str,
        trace_id: str,
        quality_status: TraceAssetStatus | str,
        reasons: Iterable[str],
        private_memory_ids: Iterable[str],
        candidate_ids: Iterable[str],
    ) -> bool:
        with self._transaction() as connection:
            result = connection.execute(
                """
                INSERT OR IGNORE INTO trace_asset_consolidations(
                    asset_id, run_id, task_id, trace_id, quality_status,
                    reasons_json, private_memory_ids_json, candidate_ids_json,
                    consolidated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    run_id,
                    task_id,
                    trace_id,
                    TraceAssetStatus(quality_status).value,
                    canonical_json(list(reasons)),
                    canonical_json(list(private_memory_ids)),
                    canonical_json(list(candidate_ids)),
                    time.time(),
                ),
            )
        return result.rowcount > 0

    def consolidate_trace_asset(
        self,
        asset: Any,
        *,
        fact_candidates: Optional[Iterable[Mapping[str, Any]]] = None,
        skill_candidates: Optional[Iterable[Mapping[str, Any]]] = None,
    ) -> Any:
        from .consolidation import MemoryConsolidator

        return MemoryConsolidator(self).consolidate(
            asset,
            fact_candidates=fact_candidates,
            skill_candidates=skill_candidates,
        )

    # -- Health and internals ---------------------------------------------

    def health(self) -> dict[str, Any]:
        with self._lock:
            journal_mode = self._connection.execute("PRAGMA journal_mode").fetchone()[0]
            foreign_keys = bool(self._connection.execute("PRAGMA foreign_keys").fetchone()[0])
            version = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
            fts_count = self._connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name LIKE '%_fts'"
            ).fetchone()[0]
        return {
            "ok": True,
            "path": str(self.path),
            "journal_mode": str(journal_mode).lower(),
            "foreign_keys": foreign_keys,
            "schema_version": int(version),
            "fts5": fts_count >= 2,
        }

    def _promote_fact(
        self,
        connection: sqlite3.Connection,
        candidate: sqlite3.Row,
        approved_by: str,
        now: float,
    ) -> str:
        payload = json.loads(candidate["payload_json"])
        fact = redact_value(payload.get("fact", payload))
        digest = content_digest({"topic": candidate["topic"], "fact": fact})
        existing = connection.execute(
            "SELECT id FROM shared_facts WHERE topic = ? AND content_hash = ?",
            (candidate["topic"], digest),
        ).fetchone()
        if existing is not None:
            return str(existing["id"])
        supersedes_id = candidate["supersedes_id"]
        if supersedes_id:
            old = connection.execute(
                "SELECT id FROM shared_facts WHERE id = ?",
                (supersedes_id,),
            ).fetchone()
            if old is None:
                raise ValueError("superseded fact does not exist")
            connection.execute(
                "UPDATE shared_facts SET retired_at = ?, updated_at = ? WHERE id = ?",
                (now, now, supersedes_id),
            )
        fact_id = f"fact:{uuid.uuid4()}"
        text = searchable_text({"topic": candidate["topic"], "fact": fact})
        connection.execute(
            """
            INSERT INTO shared_facts(
                id, topic, fact_json, searchable_text, content_hash,
                source_trace_id, source_span_id, source_event_ids_json,
                source_type, confidence, created_at, updated_at, expires_at,
                supersedes_id, retired_at, approved_at, approved_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                fact_id,
                candidate["topic"],
                canonical_json(fact),
                text,
                digest,
                candidate["source_trace_id"],
                candidate["source_span_id"],
                candidate["source_event_ids_json"],
                candidate["source_type"],
                candidate["confidence"],
                now,
                now,
                candidate["expires_at"],
                supersedes_id,
                now,
                approved_by,
            ),
        )
        connection.execute(
            """
            INSERT INTO shared_fact_fts(fact_id, topic, searchable_text)
            VALUES (?, ?, ?)
            """,
            (fact_id, candidate["topic"], text),
        )
        return fact_id

    def _private_record(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "kind": row["kind"],
            "task_id": row["task_id"],
            "content": json.loads(row["content_json"]),
            "source_trace_id": row["source_trace_id"],
            "source_span_id": row["source_span_id"],
            "source_event_ids": json.loads(row["source_event_ids_json"]),
            "source_type": row["source_type"],
            "confidence": row["confidence"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
            "supersedes_id": row["supersedes_id"],
            "retired_at": row["retired_at"],
            "search_rank": row["search_rank"] if "search_rank" in row.keys() else 0.0,
        }

    def _fact_record(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "topic": row["topic"],
            "fact": json.loads(row["fact_json"]),
            "source_trace_id": row["source_trace_id"],
            "source_span_id": row["source_span_id"],
            "source_event_ids": json.loads(row["source_event_ids_json"]),
            "source_type": row["source_type"],
            "confidence": row["confidence"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
            "supersedes_id": row["supersedes_id"],
            "retired_at": row["retired_at"],
            "approved_at": row["approved_at"],
            "approved_by": row["approved_by"],
            "search_rank": row["search_rank"] if "search_rank" in row.keys() else 0.0,
        }

    def _candidate_record(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "candidate_type": row["candidate_type"],
            "topic": row["topic"],
            "payload": json.loads(row["payload_json"]),
            "status": row["status"],
            "source_trace_id": row["source_trace_id"],
            "source_span_id": row["source_span_id"],
            "source_event_ids": json.loads(row["source_event_ids_json"]),
            "source_type": row["source_type"],
            "confidence": row["confidence"],
            "share_eligible": bool(row["share_eligible"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
            "supersedes_id": row["supersedes_id"],
            "decided_at": row["decided_at"],
            "decided_by": row["decided_by"],
            "decision_reason": row["decision_reason"],
            "promoted_memory_id": row["promoted_memory_id"],
        }

    @staticmethod
    def _confidence(value: Any) -> float:
        number = float(value)
        if number < 0.0 or number > 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return number

    class _Transaction:
        def __init__(self, store: "SQLiteMemoryStore") -> None:
            self.store = store

        def __enter__(self) -> sqlite3.Connection:
            self.store._lock.acquire()
            self.store._connection.execute("BEGIN IMMEDIATE")
            return self.store._connection

        def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            try:
                if exc_type is None:
                    self.store._connection.commit()
                else:
                    self.store._connection.rollback()
            finally:
                self.store._lock.release()

    def _transaction(self) -> "_Transaction":
        if self._closed:
            raise RuntimeError("memory store is closed")
        return self._Transaction(self)
