"""Data contracts and secret-safe serialization for persistent memory."""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence


class CandidateType(str, enum.Enum):
    FACT = "fact"
    SKILL = "skill"


class CandidateStatus(str, enum.Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class CandidateDecision(str, enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"


class TraceAssetStatus(str, enum.Enum):
    RAW = "raw"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class Provenance:
    """Pointers back to the evidence that produced a memory item."""

    source_trace_id: str
    source_span_id: str
    source_event_ids: tuple[str, ...]
    source_type: str = "trace_asset"

    @classmethod
    def from_value(
        cls,
        value: Optional[Mapping[str, Any]] = None,
        *,
        default_type: str = "legacy",
    ) -> "Provenance":
        raw = dict(value or {})
        event_ids = raw.get("source_event_ids", raw.get("event_ids", ()))
        if isinstance(event_ids, str):
            event_ids = (event_ids,)
        normalized_ids = tuple(str(item) for item in (event_ids or ()) if item)
        # The legacy MemoryPort did not require provenance.  Explicit sentinels
        # preserve compatibility without pretending that unattributed data is
        # trace-backed evidence.
        return cls(
            source_trace_id=str(
                raw.get("source_trace_id") or raw.get("trace_id") or "legacy:unattributed"
            ),
            source_span_id=str(
                raw.get("source_span_id") or raw.get("span_id") or "legacy:unattributed"
            ),
            source_event_ids=normalized_ids or ("legacy:unattributed",),
            source_type=str(raw.get("source_type") or default_type),
        )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class TraceEventRef:
    event_id: str
    event_type: str
    span_id: str
    agent_id: Optional[str] = None
    offset: Optional[int] = None
    confirmed: bool = False


@dataclass(frozen=True)
class TraceAsset:
    """A deterministic, payload-free task episode derived from one trace."""

    asset_id: str
    run_id: str
    task_id: str
    trace_id: str
    root_span_id: str
    terminal_status: str
    terminal_event_id: str
    participant_agent_ids: tuple[str, ...]
    event_refs: tuple[TraceEventRef, ...]
    span_ids: tuple[str, ...]
    started_at: float
    ended_at: float
    summary: str
    redaction_clean: bool
    status: TraceAssetStatus = TraceAssetStatus.RAW
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["status"] = self.status.value
        return value


@dataclass(frozen=True)
class QualityGateResult:
    accepted: bool
    status: TraceAssetStatus
    completeness_score: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConsolidationResult:
    asset_id: str
    quality_status: TraceAssetStatus
    private_memory_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    deduplicated: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["quality_status"] = self.quality_status.value
        return value


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "password",
    "passwd",
    "secret",
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    "token",
    "chain_of_thought",
    "hidden_reasoning",
    "reasoning_content",
    "cot",
}
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_API_KEY_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}")
_URL_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|api_key|apikey|auth|token|password)=)[^&#\s]+"
)
_ENV_SECRET_RE = re.compile(
    r"(?im)^(\s*(?:[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|COOKIE))\s*=).+$"
)


def json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {field.name: json_value(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_value(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        binary = bytes(value)
        return {
            "binary_omitted": True,
            "size": len(binary),
            "sha256": hashlib.sha256(binary).hexdigest(),
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def redact_value(value: Any) -> Any:
    """Return a JSON-safe value with credentials removed.

    Memory is not a credential store.  Redaction is deliberately repeated here
    even when the observation layer already sanitized its TraceAsset input.
    """

    if dataclasses.is_dataclass(value):
        value = json_value(value)
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS:
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_value(item)
        return redacted
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        text = _BEARER_RE.sub("Bearer [REDACTED]", value)
        text = _API_KEY_RE.sub("sk-[REDACTED]", text)
        text = _URL_SECRET_RE.sub(r"\1[REDACTED]", text)
        text = _ENV_SECRET_RE.sub(r"\1[REDACTED]", text)
        return text
    return json_value(value)


def contains_sensitive_value(value: Any) -> bool:
    """Detect raw credentials before an episode can pass its quality gate."""

    if dataclasses.is_dataclass(value):
        value = json_value(value)
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS and item not in (None, "", "[REDACTED]"):
                return True
            if contains_sensitive_value(item):
                return True
        return False
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(contains_sensitive_value(item) for item in value)
    if isinstance(value, str):
        return bool(
            _BEARER_RE.search(value)
            or _API_KEY_RE.search(value)
            or _URL_SECRET_RE.search(value)
            or _ENV_SECRET_RE.search(value)
        )
    return False


def searchable_text(value: Any) -> str:
    """Flatten safe scalar values for FTS5 indexing."""

    safe = redact_value(value)
    chunks: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                chunks.append(str(key))
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif item is not None:
            chunks.append(str(item))

    visit(safe)
    return " ".join(chunks)


def fts_query(value: str) -> str:
    """Turn untrusted user text into a conservative FTS5 prefix query."""

    tokens = re.findall(r"[\w\u3400-\u9fff]+", value, flags=re.UNICODE)
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"*' for token in tokens[:16])
