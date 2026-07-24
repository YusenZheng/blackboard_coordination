"""Unified, capability-aware Tool execution runtime."""
from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Callable, Optional

from ..contracts.agent_card import AgentCard
from ..contracts.tooling import ToolBinding, ToolCallRequest, ToolCallResult, ToolErrorCode
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
        return spec is not None and self._binding(spec, device_id).status == "available"

    def invoke(self, request: ToolCallRequest) -> ToolCallResult:
        started = time.perf_counter()
        spec = self.catalog.get(request.tool_id)
        canonical_id = spec.tool_id if spec is not None else request.tool_id
        fingerprint = self._fingerprint(request, canonical_id)
        cached = self._cached(request, fingerprint, canonical_id)
        if cached is not None:
            self._emit_trace(request, spec, cached, cached=True)
            return cached
        binding = None
        try:
            if spec is None:
                result = self._failure(request, canonical_id, ToolErrorCode.NOT_FOUND, "tool is not registered")
            elif not spec.implemented:
                result = self._failure(request, canonical_id, ToolErrorCode.UNAVAILABLE, "tool is not implemented")
            else:
                errors = _validate_schema(request.arguments, spec.input_schema)
                if errors:
                    result = self._failure(request, canonical_id, ToolErrorCode.INVALID_ARGUMENT, "; ".join(errors))
                else:
                    binding = self._binding(spec, request.device_id)
                    if binding.status != "available":
                        result = self._failure(
                            request, canonical_id, ToolErrorCode.UNAVAILABLE,
                            ",".join(binding.reason_codes) or "tool unavailable", binding=binding,
                        )
                    else:
                        result = self._execute(spec, request, binding)
        except TimeoutError as exc:
            result = self._failure(request, canonical_id, ToolErrorCode.OUTCOME_UNKNOWN, str(exc) or "tool outcome is unknown", outcome_certainty="unknown", binding=binding)
        except Exception as exc:
            result = self._failure(request, canonical_id, ToolErrorCode.EXECUTION_FAILED, f"{type(exc).__name__}: {exc}", binding=binding)
        result.duration_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
        self._remember(request, fingerprint, result)
        self._emit_trace(request, spec, result, cached=False)
        return result

    def _binding(self, spec: ToolSpec, device_id: Optional[str]) -> ToolBinding:
        if spec.executor_type != "device":
            available = (
                (spec.executor_type == "platform" and spec.run is not None)
                or (spec.executor_type == "blackboard" and self.blackboard is not None)
                or (spec.executor_type == "registry" and self.device_registry is not None)
            )
            return ToolBinding(device_id or "platform", spec.tool_id, spec.executor_type, None,
                               "available" if available else "unavailable",
                               [] if available else ["EXECUTOR_UNAVAILABLE"])
        reasons: list[str] = []
        card: Optional[AgentCard] = (
            self.device_registry.get(device_id) if self.device_registry is not None and device_id else None
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
            if spec.action_verb and spec.action_verb not in {_enum_value(item) for item in card.capability.action_verbs}:
                reasons.append("ACTION_VERB_UNSUPPORTED")
        adapter = self.adapters.get(device_id or "")
        if adapter is None:
            reasons.append("ADAPTER_NOT_FOUND")
        return ToolBinding(device_id or "", spec.tool_id, spec.executor_type,
                           type(adapter).__name__ if adapter is not None else None,
                           "available" if not reasons else "unavailable",
                           list(dict.fromkeys(reasons)))

    def _execute(self, spec: ToolSpec, request: ToolCallRequest, binding: ToolBinding) -> ToolCallResult:
        receipt = None
        if spec.executor_type == "device":
            if request.action_intent is not None:
                receipt = self.adapters[request.device_id].execute(request.action_intent)
                value = dict(receipt.result)
                success = bool(receipt.success)
                error_code = None if success else ToolErrorCode.EXECUTION_FAILED.value
                error_message = receipt.failure_reason
            elif spec.run is not None:
                value = dict(spec.run(dict(request.arguments)))
                success, error_code, error_message = True, None, None
            else:
                return self._failure(request, spec.tool_id, ToolErrorCode.INVALID_ARGUMENT, "device tool requires action_intent", binding=binding)
        elif spec.executor_type == "platform":
            value = dict(spec.run(dict(request.arguments)))
            success, error_code, error_message = True, None, None
        elif spec.executor_type == "blackboard":
            owner = request.agent_id or request.device_id
            backend = getattr(self.blackboard, "backend", self.blackboard)
            acquired = bool(backend.claim(str(request.arguments["resource"]), owner))
            value = {"resource": request.arguments["resource"], "owner": owner, "acquired": acquired}
            success, error_code, error_message = acquired, None, None
        elif spec.executor_type == "registry":
            device_id = request.arguments.get("device_id")
            cards = ([self.device_registry.get(device_id)] if device_id else list(self.device_registry.all_cards()))
            value = {"devices": [
                {"device_id": card.identity.device_id, "device_type": _enum_value(card.identity.device_type),
                 "online": card.state.online, "healthy": card.state.healthy,
                 "capabilities": list(card.capability.profile.capabilities),
                 "action_verbs": [_enum_value(item) for item in card.capability.action_verbs]}
                for card in cards if card is not None
            ]}
            success, error_code, error_message = True, None, None
        else:
            return self._failure(request, spec.tool_id, ToolErrorCode.UNAVAILABLE, "executor type is unsupported", binding=binding)
        output_errors = _validate_schema(value, spec.output_schema)
        if output_errors:
            return self._failure(request, spec.tool_id, ToolErrorCode.EXECUTION_FAILED, "; ".join(output_errors), binding=binding)
        return ToolCallResult("2.0", request.call_id, spec.tool_id, success, value, error_code,
                              error_message, device_id=request.device_id, receipt=receipt, binding=binding)

    def _cached(self, request, fingerprint, tool_id):
        if not request.idempotency_key:
            return None
        existing = self._idempotency.get(request.idempotency_key)
        if existing is None:
            return None
        previous_fingerprint, previous_result = existing
        if previous_fingerprint == fingerprint:
            return copy.deepcopy(previous_result)
        return self._failure(request, tool_id, ToolErrorCode.IDEMPOTENCY_CONFLICT, "idempotency key was already used with different arguments")

    def _remember(self, request, fingerprint, result):
        if request.idempotency_key and result.error_code != ToolErrorCode.IDEMPOTENCY_CONFLICT.value:
            self._idempotency.setdefault(request.idempotency_key, (fingerprint, copy.deepcopy(result)))

    @staticmethod
    def _fingerprint(request, canonical_id):
        return json.dumps({"tool_id": canonical_id, "arguments": request.arguments, "device_id": request.device_id, "task_id": request.task_id}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _failure(request, tool_id, code, message, *, outcome_certainty="confirmed", binding=None):
        return ToolCallResult("2.0", request.call_id, tool_id, False, error_code=code.value,
                              error_message=message, retryable=code in (ToolErrorCode.TIMEOUT, ToolErrorCode.UNAVAILABLE),
                              outcome_certainty=outcome_certainty, device_id=request.device_id, binding=binding)

    def _emit_trace(self, request, spec, result, *, cached):
        if self.trace_listener is None:
            return
        value = {"trace_id": request.trace_id or request.call_id, "call_id": request.call_id,
                 "task_id": request.task_id, "device_id": request.device_id, "tool_id": result.tool_id,
                 "tool_name": spec.name if spec else None, "tool_version": spec.version if spec else None,
                 "tool_class": spec.tool_class if spec else None, "executor_type": spec.executor_type if spec else None,
                 "success": result.success, "error_code": result.error_code,
                 "duration_ms": result.duration_ms, "cached": cached,
                 "arguments": _json_value(request.arguments), "result": _json_value(result.result),
                 "binding": _json_value(result.binding),
                 "skill_references": _json_value(request.context.get("skill_references", []))}
        try:
            self.trace_listener(value)
        except Exception:
            pass


def _validate_schema(value, schema: dict, path: str = "arguments") -> list[str]:
    if not schema or "type" not in schema:
        return []
    expected = schema["type"]
    types = {"object": dict, "array": list, "string": str, "number": (int, float), "integer": int, "boolean": bool}
    if expected in types and (not isinstance(value, types[expected]) or (expected == "number" and isinstance(value, bool))):
        return [f"{path} must be {expected}"]
    if expected == "object" and isinstance(value, dict):
        errors = [f"{path}.{name} is required" for name in schema.get("required", []) if name not in value]
        for name, child in schema.get("properties", {}).items():
            if name in value and isinstance(child, dict):
                errors.extend(_validate_schema(value[name], child, f"{path}.{name}"))
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
