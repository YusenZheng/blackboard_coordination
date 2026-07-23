"""Unified, capability-aware Tool execution runtime."""
from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Callable, Optional

from ..contracts.agent_card import AgentCard
from ..contracts.task import ActionReceipt
from ..contracts.tooling import (
    ToolBinding,
    ToolCallRequest,
    ToolCallResult,
    ToolErrorCode,
)
from .capability_matcher import CapabilityMatcher
from .tools.base import ToolRegistry, ToolSpec


ToolTraceListener = Callable[[dict], None]


class ToolRuntime:
    def __init__(
        self,
        *,
        catalog: ToolRegistry,
        device_registry=None,
        adapters: Optional[dict] = None,
        blackboard=None,
        trace_listener: Optional[ToolTraceListener] = None,
    ) -> None:
        self.catalog = catalog
        self.device_registry = device_registry
        self.adapters = adapters or {}
        self.blackboard = blackboard
        self.trace_listener = trace_listener
        self.matcher = CapabilityMatcher()
        self._idempotency: dict[str, tuple[str, ToolCallResult]] = {}

    def is_available(self, device_id: str, verb: str) -> bool:
        spec = self.catalog.for_action_verb(verb)
        if spec is None:
            return False
        return self._binding(spec, device_id).status == "available"

    def invoke(self, request: ToolCallRequest) -> ToolCallResult:
        started = time.perf_counter()
        spec = self.catalog.get(request.tool_id)
        canonical_id = spec.tool_id if spec is not None else request.tool_id
        fingerprint = self._fingerprint(request, canonical_id)
        cached = self._cached(request, fingerprint, canonical_id)
        if cached is not None:
            self._emit_trace(request, spec, cached, cached=True)
            return cached

        binding: Optional[ToolBinding] = None
        try:
            if spec is None:
                result = self._failure(
                    request, canonical_id, ToolErrorCode.NOT_FOUND, "tool is not registered"
                )
            elif not spec.implemented:
                result = self._failure(
                    request, canonical_id, ToolErrorCode.UNAVAILABLE, "tool is not implemented"
                )
            else:
                errors = _validate_schema(request.arguments, spec.input_schema)
                if errors:
                    result = self._failure(
                        request,
                        canonical_id,
                        ToolErrorCode.INVALID_ARGUMENT,
                        "; ".join(errors),
                    )
                else:
                    binding = self._binding(spec, request.device_id)
                    if binding.status != "available":
                        result = self._failure(
                            request,
                            canonical_id,
                            ToolErrorCode.UNAVAILABLE,
                            ",".join(binding.reason_codes) or "tool unavailable",
                            binding=binding,
                        )
                    else:
                        result = self._execute(spec, request, binding)
        except TimeoutError as exc:
            result = self._failure(
                request,
                canonical_id,
                ToolErrorCode.OUTCOME_UNKNOWN,
                str(exc) or "tool outcome is unknown",
                outcome_certainty="unknown",
                binding=binding,
            )
        except Exception as exc:
            result = self._failure(
                request,
                canonical_id,
                ToolErrorCode.EXECUTION_FAILED,
                f"{type(exc).__name__}: {exc}",
                binding=binding,
            )

        result.duration_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
        self._remember(request, fingerprint, result)
        self._emit_trace(request, spec, result, cached=False)
        return result

    def _binding(self, spec: ToolSpec, device_id: Optional[str]) -> ToolBinding:
        if (
            spec.executor_type == "device"
            and not device_id
            and spec.run is not None
        ):
            return ToolBinding(
                device_id="",
                tool_id=spec.tool_id,
                executor_id="legacy_direct",
                adapter_id=None,
                status="available",
            )
        if spec.executor_type != "device":
            available = (
                (spec.executor_type == "platform" and spec.run is not None)
                or (spec.executor_type == "blackboard" and self.blackboard is not None)
                or (spec.executor_type == "registry" and self.device_registry is not None)
            )
            return ToolBinding(
                device_id=device_id or "platform",
                tool_id=spec.tool_id,
                executor_id=spec.executor_type,
                adapter_id=None,
                status="available" if available else "unavailable",
                reason_codes=[] if available else ["EXECUTOR_UNAVAILABLE"],
            )

        reasons: list[str] = []
        card: Optional[AgentCard] = (
            self.device_registry.get(device_id)
            if self.device_registry is not None and device_id
            else None
        )
        if card is None:
            reasons.append("AGENT_CARD_NOT_FOUND")
        else:
            match = self.matcher.match(card, spec.requirements)
            reasons.extend(match.reason_codes)
            if not card.state.online:
                reasons.append("AGENT_OFFLINE")
            if not card.state.healthy:
                reasons.append("AGENT_UNHEALTHY")
            if spec.action_verb and spec.action_verb not in {
                _enum_value(item) for item in card.capability.action_verbs
            }:
                reasons.append("ACTION_VERB_UNSUPPORTED")
        adapter = self.adapters.get(device_id or "")
        if adapter is None:
            reasons.append("ADAPTER_NOT_FOUND")
        return ToolBinding(
            device_id=device_id or "",
            tool_id=spec.tool_id,
            executor_id=spec.executor_type,
            adapter_id=type(adapter).__name__ if adapter is not None else None,
            status="available" if not reasons else "unavailable",
            reason_codes=list(dict.fromkeys(reasons)),
        )

    def _execute(
        self, spec: ToolSpec, request: ToolCallRequest, binding: ToolBinding
    ) -> ToolCallResult:
        receipt = None
        if spec.executor_type == "device":
            if request.action_intent is None:
                if binding.executor_id != "legacy_direct" or spec.run is None:
                    return self._failure(
                        request,
                        spec.tool_id,
                        ToolErrorCode.INVALID_ARGUMENT,
                        "device tool requires action_intent",
                        binding=binding,
                    )
                value = dict(spec.run(dict(request.arguments)))
                success, error_code, error_message = True, None, None
            else:
                receipt = self.adapters[request.device_id].execute(request.action_intent)
                value = dict(receipt.result)
                success = bool(receipt.success)
                error_message = receipt.failure_reason
                error_code = None if success else ToolErrorCode.EXECUTION_FAILED.value
        elif spec.executor_type == "platform":
            value = dict(spec.run(dict(request.arguments)))
            success, error_code, error_message = True, None, None
        elif spec.executor_type == "blackboard":
            value = self._execute_blackboard(spec, request)
            success, error_code, error_message = bool(value.get("acquired")), None, None
        elif spec.executor_type == "registry":
            value = self._execute_registry(request)
            success, error_code, error_message = True, None, None
        else:
            return self._failure(
                request,
                spec.tool_id,
                ToolErrorCode.UNAVAILABLE,
                "executor type is unsupported",
                binding=binding,
            )
        output_errors = _validate_schema(value, spec.output_schema)
        if output_errors:
            return self._failure(
                request,
                spec.tool_id,
                ToolErrorCode.EXECUTION_FAILED,
                "; ".join(output_errors),
                binding=binding,
            )
        return ToolCallResult(
            schema_version="2.0",
            call_id=request.call_id,
            tool_id=spec.tool_id,
            success=success,
            result=value,
            error_code=error_code,
            error_message=error_message,
            device_id=request.device_id,
            receipt=receipt,
            binding=binding,
        )

    def _execute_blackboard(self, spec: ToolSpec, request: ToolCallRequest) -> dict:
        if spec.tool_id != "C03":
            raise ValueError("unsupported blackboard tool")
        owner = request.agent_id or request.device_id
        if not owner:
            raise ValueError("claim requires agent_id or device_id")
        backend = getattr(self.blackboard, "backend", self.blackboard)
        acquired = bool(backend.claim(str(request.arguments["resource"]), owner))
        return {"resource": request.arguments["resource"], "owner": owner, "acquired": acquired}

    def _execute_registry(self, request: ToolCallRequest) -> dict:
        device_id = request.arguments.get("device_id")
        cards = (
            [self.device_registry.get(device_id)]
            if device_id
            else list(self.device_registry.all_cards())
        )
        return {
            "devices": [
                {
                    "device_id": card.identity.device_id,
                    "device_type": _enum_value(card.identity.device_type),
                    "online": card.state.online,
                    "healthy": card.state.healthy,
                    "capabilities": list(card.capability.profile.capabilities),
                    "action_verbs": [
                        _enum_value(item) for item in card.capability.action_verbs
                    ],
                }
                for card in cards
                if card is not None
            ]
        }

    def _cached(
        self, request: ToolCallRequest, fingerprint: str, tool_id: str
    ) -> Optional[ToolCallResult]:
        if not request.idempotency_key:
            return None
        existing = self._idempotency.get(request.idempotency_key)
        if existing is None:
            return None
        previous_fingerprint, previous_result = existing
        if previous_fingerprint == fingerprint:
            return copy.deepcopy(previous_result)
        return self._failure(
            request,
            tool_id,
            ToolErrorCode.IDEMPOTENCY_CONFLICT,
            "idempotency key was already used with different arguments",
        )

    def _remember(
        self, request: ToolCallRequest, fingerprint: str, result: ToolCallResult
    ) -> None:
        if request.idempotency_key and result.error_code != ToolErrorCode.IDEMPOTENCY_CONFLICT.value:
            self._idempotency.setdefault(
                request.idempotency_key, (fingerprint, copy.deepcopy(result))
            )

    @staticmethod
    def _fingerprint(request: ToolCallRequest, canonical_id: str) -> str:
        return json.dumps(
            {
                "tool_id": canonical_id,
                "arguments": request.arguments,
                "device_id": request.device_id,
                "task_id": request.task_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _failure(
        request: ToolCallRequest,
        tool_id: str,
        code: ToolErrorCode,
        message: str,
        *,
        outcome_certainty: str = "confirmed",
        binding: Optional[ToolBinding] = None,
    ) -> ToolCallResult:
        return ToolCallResult(
            schema_version="2.0",
            call_id=request.call_id,
            tool_id=tool_id,
            success=False,
            error_code=code.value,
            error_message=message,
            retryable=code in (ToolErrorCode.TIMEOUT, ToolErrorCode.UNAVAILABLE),
            outcome_certainty=outcome_certainty,
            device_id=request.device_id,
            binding=binding,
        )

    def _emit_trace(
        self,
        request: ToolCallRequest,
        spec: Optional[ToolSpec],
        result: ToolCallResult,
        *,
        cached: bool,
    ) -> None:
        if self.trace_listener is None:
            return
        value = {
            "trace_id": request.trace_id or request.call_id,
            "call_id": request.call_id,
            "task_id": request.task_id,
            "agent_id": request.agent_id,
            "device_id": request.device_id,
            "tool_id": result.tool_id,
            "tool_name": spec.name if spec is not None else None,
            "tool_version": spec.version if spec is not None else None,
            "tool_class": spec.tool_class if spec is not None else None,
            "executor_type": spec.executor_type if spec is not None else None,
            "success": result.success,
            "error_code": result.error_code,
            "outcome_certainty": result.outcome_certainty,
            "duration_ms": result.duration_ms,
            "cached": cached,
            "arguments": _json_value(request.arguments),
            "result": _json_value(result.result),
            "binding": _json_value(result.binding),
            "skill_references": _json_value(
                request.context.get("skill_references", [])
            ),
            "environment": request.context.get("environment", "mock"),
        }
        try:
            self.trace_listener(value)
        except Exception:
            return


def _validate_schema(value, schema: dict, path: str = "arguments") -> list[str]:
    if not schema:
        return []
    expected = schema.get("type")
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
    }
    if expected in types and (not isinstance(value, types[expected]) or (
        expected == "number" and isinstance(value, bool)
    )):
        return [f"{path} must be {expected}"]
    if expected == "object" and isinstance(value, dict):
        errors = [
            f"{path}.{name} is required"
            for name in schema.get("required", [])
            if name not in value
        ]
        properties = schema.get("properties", {})
        for name, child_schema in properties.items():
            if name in value and isinstance(child_schema, dict):
                errors.extend(_validate_schema(value[name], child_schema, f"{path}.{name}"))
        return errors
    return []


def _enum_value(value) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def _json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    return str(value)
