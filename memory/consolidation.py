"""Deterministic TraceAsset construction and memory consolidation."""

from __future__ import annotations

import dataclasses
import hashlib
import time
from typing import Any, Iterable, Mapping, Optional, Sequence

from .models import (
    ConsolidationResult,
    Provenance,
    QualityGateResult,
    TraceAsset,
    TraceAssetStatus,
    TraceEventRef,
    canonical_json,
    contains_sensitive_value,
    json_value,
)


_TERMINAL_TYPES = {"task_done", "task_failed"}
_CONFIRMED_RECEIPT_TYPES = {"receipt", "assignment_completed"}
_FACT_EVIDENCE_TYPES = {
    "structured_task",
    "authoritative_evidence",
    "confirmed_receipt",
    "human_confirmation",
}


class TaskEpisodeBuilder:
    """Build a small, deterministic and payload-free asset from one run."""

    def build(
        self,
        *,
        run_id: str,
        task_id: str,
        trace_id: str,
        events: Sequence[Any],
        spans: Sequence[Any],
        terminal_status: Optional[str] = None,
        started_at: Optional[float] = None,
        ended_at: Optional[float] = None,
        participant_agent_ids: Optional[Iterable[str]] = None,
    ) -> TraceAsset:
        raw_events = [json_value(item) for item in events]
        raw_spans = [json_value(item) for item in spans]
        sorted_events = sorted(raw_events, key=self._event_sort_key)
        sorted_spans = sorted(raw_spans, key=self._span_sort_key)
        event_refs: list[TraceEventRef] = []
        discovered_agents: set[str] = set(participant_agent_ids or ())
        terminal_event_id = ""
        inferred_terminal = terminal_status or ""
        for index, event in enumerate(sorted_events):
            if not isinstance(event, Mapping):
                continue
            event_id = str(
                self._first(event, "event_id", "id")
                or f"missing:event:{index}"
            )
            event_type = str(
                self._first(event, "event_type", "type", "name") or "unknown"
            )
            span_id = str(self._first(event, "span_id", "source_span_id") or "")
            agent_id = self._first(event, "agent_id", "device_id")
            if agent_id:
                discovered_agents.add(str(agent_id))
            content = event.get("content")
            if isinstance(content, Mapping):
                nested_agent = self._first(content, "agent_id", "device_id")
                if nested_agent:
                    discovered_agents.add(str(nested_agent))
            if event_type in _TERMINAL_TYPES:
                terminal_event_id = event_id
                inferred_terminal = "success" if event_type == "task_done" else "failed"
            event_refs.append(
                TraceEventRef(
                    event_id=event_id,
                    event_type=event_type,
                    span_id=span_id,
                    agent_id=str(agent_id) if agent_id else None,
                    offset=self._integer_or_none(event.get("offset")),
                    confirmed=event_type in _CONFIRMED_RECEIPT_TYPES,
                )
            )
        span_ids = tuple(
            str(self._first(span, "span_id", "id"))
            for span in sorted_spans
            if isinstance(span, Mapping) and self._first(span, "span_id", "id")
        )
        root_span_id = ""
        for span in sorted_spans:
            if not isinstance(span, Mapping):
                continue
            parent = self._first(span, "parent_span_id", "parent_id")
            if not parent:
                root_span_id = str(self._first(span, "span_id", "id") or "")
                if root_span_id:
                    break
        if not root_span_id and span_ids:
            root_span_id = span_ids[0]
        started = (
            float(started_at)
            if started_at is not None
            else self._minimum_time(sorted_spans, sorted_events)
        )
        ended = (
            float(ended_at)
            if ended_at is not None
            else self._maximum_time(sorted_spans, sorted_events, started)
        )
        event_sequence = ",".join(ref.event_type for ref in event_refs)
        summary = (
            f"task={task_id}; outcome={inferred_terminal or 'unknown'}; "
            f"events={event_sequence}"
        )
        identity = {
            "run_id": run_id,
            "task_id": task_id,
            "trace_id": trace_id,
            "event_ids": [ref.event_id for ref in event_refs],
            "span_ids": span_ids,
        }
        asset_id = "trace-asset:" + hashlib.sha256(
            canonical_json(identity).encode("utf-8")
        ).hexdigest()
        return TraceAsset(
            asset_id=asset_id,
            run_id=str(run_id),
            task_id=str(task_id),
            trace_id=str(trace_id),
            root_span_id=root_span_id,
            terminal_status=str(inferred_terminal),
            terminal_event_id=terminal_event_id,
            participant_agent_ids=tuple(sorted(discovered_agents)),
            event_refs=tuple(event_refs),
            span_ids=span_ids,
            started_at=started,
            ended_at=max(started, ended),
            summary=summary[:2000],
            redaction_clean=not contains_sensitive_value(
                {"events": raw_events, "spans": raw_spans}
            ),
        )

    def from_trace(self, trace: Mapping[str, Any]) -> TraceAsset:
        run = trace.get("run") if isinstance(trace.get("run"), Mapping) else {}
        return self.build(
            run_id=str(trace.get("run_id") or run.get("run_id") or ""),
            task_id=str(trace.get("task_id") or run.get("task_id") or ""),
            trace_id=str(trace.get("trace_id") or run.get("trace_id") or ""),
            events=list(trace.get("events") or trace.get("timeline") or ()),
            spans=list(trace.get("spans") or ()),
            terminal_status=trace.get("terminal_status") or run.get("status"),
            started_at=trace.get("started_at") or run.get("started_at"),
            ended_at=trace.get("ended_at") or run.get("ended_at"),
            participant_agent_ids=trace.get("participant_agent_ids"),
        )

    @staticmethod
    def _first(value: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            if value.get(name) is not None:
                return value[name]
        return None

    @classmethod
    def _event_sort_key(cls, value: Any) -> tuple[float, int, str]:
        if not isinstance(value, Mapping):
            return (0.0, 0, str(value))
        timestamp = cls._number(
            cls._first(value, "timestamp", "ts", "created_at", "start_time")
        )
        offset = cls._integer_or_none(value.get("offset")) or 0
        identifier = str(cls._first(value, "event_id", "id") or "")
        return (timestamp, offset, identifier)

    @classmethod
    def _span_sort_key(cls, value: Any) -> tuple[float, str]:
        if not isinstance(value, Mapping):
            return (0.0, str(value))
        return (
            cls._number(cls._first(value, "start_time", "started_at", "timestamp")),
            str(cls._first(value, "span_id", "id") or ""),
        )

    @classmethod
    def _minimum_time(cls, spans: Sequence[Any], events: Sequence[Any]) -> float:
        values: list[float] = []
        for item in [*spans, *events]:
            if isinstance(item, Mapping):
                number = cls._number(
                    cls._first(item, "start_time", "started_at", "timestamp", "ts")
                )
                if number:
                    values.append(number)
        return min(values) if values else time.time()

    @classmethod
    def _maximum_time(
        cls,
        spans: Sequence[Any],
        events: Sequence[Any],
        fallback: float,
    ) -> float:
        values: list[float] = []
        for item in [*spans, *events]:
            if isinstance(item, Mapping):
                number = cls._number(
                    cls._first(
                        item,
                        "end_time",
                        "ended_at",
                        "timestamp",
                        "ts",
                        "start_time",
                    )
                )
                if number:
                    values.append(number)
        return max(values) if values else fallback

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _integer_or_none(value: Any) -> Optional[int]:
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None


class TraceAssetQualityGate:
    """Deterministic completeness, provenance, redaction and dedupe gate."""

    def evaluate(self, asset: TraceAsset, *, duplicate: bool = False) -> QualityGateResult:
        checks = {
            "run_id_missing": bool(asset.run_id),
            "task_id_missing": bool(asset.task_id),
            "trace_id_missing": bool(asset.trace_id),
            "root_span_missing": bool(asset.root_span_id),
            "events_missing": bool(asset.event_refs),
            "terminal_event_missing": bool(asset.terminal_event_id),
            "terminal_status_missing": bool(asset.terminal_status),
            "participants_missing": bool(asset.participant_agent_ids),
            "event_provenance_incomplete": all(
                bool(ref.event_id) and not ref.event_id.startswith("missing:")
                for ref in asset.event_refs
            ),
            "sensitive_payload_detected": asset.redaction_clean,
            "duplicate_asset": not duplicate,
        }
        reasons = tuple(name for name, passed in checks.items() if not passed)
        score_checks = [
            checks["run_id_missing"],
            checks["task_id_missing"],
            checks["trace_id_missing"],
            checks["root_span_missing"],
            checks["events_missing"],
            checks["terminal_event_missing"],
            checks["terminal_status_missing"],
            checks["event_provenance_incomplete"],
        ]
        completeness = sum(1 for passed in score_checks if passed) / len(score_checks)
        accepted = not reasons
        return QualityGateResult(
            accepted=accepted,
            status=TraceAssetStatus.APPROVED if accepted else TraceAssetStatus.REJECTED,
            completeness_score=completeness,
            reasons=reasons,
        )


class MemoryConsolidator:
    """Promote a qualified task episode into private memories and candidates."""

    def __init__(self, store: Any, gate: Optional[TraceAssetQualityGate] = None) -> None:
        self.store = store
        self.gate = gate or TraceAssetQualityGate()

    def consolidate(
        self,
        asset: TraceAsset,
        *,
        fact_candidates: Optional[Iterable[Mapping[str, Any]]] = None,
        skill_candidates: Optional[Iterable[Mapping[str, Any]]] = None,
    ) -> ConsolidationResult:
        existing = self.store.get_consolidation(asset.asset_id)
        if existing is not None:
            return ConsolidationResult(
                asset_id=asset.asset_id,
                quality_status=TraceAssetStatus(existing["quality_status"]),
                private_memory_ids=tuple(existing["private_memory_ids"]),
                candidate_ids=tuple(existing["candidate_ids"]),
                reasons=tuple(existing["reasons"]),
                deduplicated=True,
            )
        gate_result = self.gate.evaluate(asset, duplicate=False)
        if not gate_result.accepted:
            self.store.record_consolidation(
                asset_id=asset.asset_id,
                run_id=asset.run_id,
                task_id=asset.task_id,
                trace_id=asset.trace_id,
                quality_status=gate_result.status,
                reasons=gate_result.reasons,
                private_memory_ids=(),
                candidate_ids=(),
            )
            return ConsolidationResult(
                asset_id=asset.asset_id,
                quality_status=gate_result.status,
                reasons=gate_result.reasons,
            )

        event_ids = tuple(ref.event_id for ref in asset.event_refs)
        provenance = Provenance(
            source_trace_id=asset.trace_id,
            source_span_id=asset.root_span_id,
            source_event_ids=event_ids,
            source_type="trace_asset",
        )
        private_ids: list[str] = []
        for agent_id in asset.participant_agent_ids:
            record = self.store.put_private(
                agent_id,
                "episodic",
                {
                    "task_id": asset.task_id,
                    "content": {
                        "asset_id": asset.asset_id,
                        "run_id": asset.run_id,
                        "task_id": asset.task_id,
                        "outcome": asset.terminal_status,
                        "terminal_event_id": asset.terminal_event_id,
                        "event_types": [ref.event_type for ref in asset.event_refs],
                        "summary": asset.summary,
                    },
                },
                provenance=provenance,
                confidence=1.0,
                task_id=asset.task_id,
            )
            private_ids.append(record["id"])

        candidate_ids: list[str] = []
        for proposal in fact_candidates or ():
            source_type = str(proposal.get("source_type") or "")
            if source_type not in _FACT_EVIDENCE_TYPES:
                # LLM prose and untyped summaries are never facts.
                continue
            candidate_provenance = self._proposal_provenance(
                proposal, asset, source_type=source_type
            )
            candidate = self.store.submit_fact_candidate(
                topic=str(proposal.get("topic") or asset.task_id),
                fact=dict(proposal.get("fact") or proposal.get("payload") or {}),
                provenance=candidate_provenance,
                confidence=float(proposal.get("confidence", 0.5)),
                source_type=source_type,
                expires_at=proposal.get("expires_at"),
                supersedes_id=proposal.get("supersedes_id"),
            )
            candidate_ids.append(candidate["id"])
        for proposal in skill_candidates or ():
            candidate = self.store.submit_skill_candidate(
                topic=str(proposal.get("topic") or asset.task_id),
                skill=dict(proposal.get("skill") or proposal.get("payload") or {}),
                provenance=self._proposal_provenance(
                    proposal, asset, source_type="trace_asset"
                ),
                confidence=float(proposal.get("confidence", 0.5)),
                expires_at=proposal.get("expires_at"),
                supersedes_id=proposal.get("supersedes_id"),
            )
            candidate_ids.append(candidate["id"])

        self.store.record_consolidation(
            asset_id=asset.asset_id,
            run_id=asset.run_id,
            task_id=asset.task_id,
            trace_id=asset.trace_id,
            quality_status=TraceAssetStatus.APPROVED,
            reasons=(),
            private_memory_ids=private_ids,
            candidate_ids=candidate_ids,
        )
        return ConsolidationResult(
            asset_id=asset.asset_id,
            quality_status=TraceAssetStatus.APPROVED,
            private_memory_ids=tuple(private_ids),
            candidate_ids=tuple(candidate_ids),
        )

    @staticmethod
    def _proposal_provenance(
        proposal: Mapping[str, Any],
        asset: TraceAsset,
        *,
        source_type: str,
    ) -> Provenance:
        raw = proposal.get("provenance")
        if isinstance(raw, Mapping):
            supplied = Provenance.from_value(raw, default_type=source_type)
            return Provenance(
                supplied.source_trace_id,
                supplied.source_span_id,
                supplied.source_event_ids,
                source_type,
            )
        return Provenance(
            asset.trace_id,
            asset.root_span_id,
            tuple(ref.event_id for ref in asset.event_refs),
            source_type,
        )
