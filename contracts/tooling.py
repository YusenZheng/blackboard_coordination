"""Stable contracts for capability-aware Tool invocation."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .task import ActionIntent, ActionReceipt


class ToolErrorCode(str, Enum):
    NOT_FOUND = "NOT_FOUND"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNAVAILABLE = "UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True)
class CapabilityRequirement:
    all_of: frozenset[str] = frozenset()
    any_of: tuple[frozenset[str], ...] = ()
    constraints: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityMatchResult:
    matched: bool
    missing_capabilities: list[str] = field(default_factory=list)
    unsatisfied_any_of: list[list[str]] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolBinding:
    device_id: str
    tool_id: str
    executor_id: str
    adapter_id: Optional[str]
    status: str
    reason_codes: list[str] = field(default_factory=list)


@dataclass
class ToolCallRequest:
    schema_version: str
    call_id: str
    tool_id: str
    arguments: dict
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    device_id: Optional[str] = None
    trace_id: Optional[str] = None
    attempt: int = 1
    idempotency_key: Optional[str] = None
    action_intent: Optional[ActionIntent] = None
    context: dict = field(default_factory=dict)


@dataclass
class ToolCallResult:
    schema_version: str
    call_id: str
    tool_id: str
    success: bool
    result: dict = field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    retryable: bool = False
    outcome_certainty: str = "confirmed"
    duration_ms: float = 0.0
    device_id: Optional[str] = None
    receipt: Optional[ActionReceipt] = None
    binding: Optional[ToolBinding] = None
