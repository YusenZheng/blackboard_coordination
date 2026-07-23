# STATUS: STAGED(A类)—— DeepSeek V4 统一客户端与三类可注入 LLM 策略
"""DeepSeek V4 runtime adapters.

密钥只从进程环境或被 Git 忽略的 ``.env.local`` 读取。该模块位于 runtime，
可以装配 ingress 和 coordination，但业务层不反向依赖具体模型厂商。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..coordination.models import (
    CandidateAssignment,
    CandidateAssignmentPlan,
    CollaborationProposal,
    GroupPlanningInput,
    to_json_value,
)


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
SUPPORTED_V4_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}


class DeepSeekError(RuntimeError):
    """DeepSeek 接入的稳定错误基类。"""


class DeepSeekConfigurationError(DeepSeekError):
    pass


class DeepSeekResponseError(DeepSeekError):
    pass


class JsonTransportPort(Protocol):
    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> dict[str, Any]: ...

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]: ...


DeepSeekTelemetryListener = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_s: float = 30.0
    max_tokens: int = 2048

    def __post_init__(self) -> None:
        normalized_key = self.api_key.strip()
        object.__setattr__(self, "api_key", normalized_key)
        if re.fullmatch(r"sk-[A-Za-z0-9_-]{20,}", normalized_key) is None:
            raise DeepSeekConfigurationError(
                "DEEPSEEK_API_KEY must contain the complete sk-... value"
            )
        if not self.base_url.startswith("https://"):
            raise DeepSeekConfigurationError("DEEPSEEK_BASE_URL must use https")
        if self.model not in SUPPORTED_V4_MODELS:
            raise DeepSeekConfigurationError(
                "DEEPSEEK_MODEL must be deepseek-v4-flash or deepseek-v4-pro"
            )
        if self.timeout_s <= 0:
            raise DeepSeekConfigurationError("DEEPSEEK_TIMEOUT_S must be positive")
        if self.max_tokens <= 0:
            raise DeepSeekConfigurationError("DEEPSEEK_MAX_TOKENS must be positive")

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        env_file: Optional[str | os.PathLike[str]] = None,
    ) -> "DeepSeekConfig":
        file_path = (
            Path(env_file)
            if env_file is not None
            else Path(__file__).resolve().parents[1] / ".env.local"
        )
        values = _read_env_file(file_path)
        values.update(dict(os.environ if env is None else env))
        try:
            timeout_s = float(values.get("DEEPSEEK_TIMEOUT_S", "30"))
            max_tokens = int(values.get("DEEPSEEK_MAX_TOKENS", "2048"))
        except ValueError as exc:
            raise DeepSeekConfigurationError(
                "DeepSeek numeric configuration is invalid"
            ) from exc
        return cls(
            api_key=values.get("DEEPSEEK_API_KEY", ""),
            base_url=values.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            model=values.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
            timeout_s=timeout_s,
            max_tokens=max_tokens,
        )


class UrllibJsonTransport:
    """零第三方依赖的 OpenAI-compatible JSON transport。"""

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> dict[str, Any]:
        request = Request(url, headers=dict(headers), method="GET")
        return self._send(request, timeout_s)

    def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        return self._send(request, timeout_s)

    @staticmethod
    def _send(request: Request, timeout_s: float) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read(2048).decode("utf-8", errors="replace")
            raise DeepSeekResponseError(
                f"DeepSeek HTTP {exc.code}: {_safe_remote_message(body)}"
            ) from exc
        except URLError as exc:
            raise DeepSeekResponseError(
                f"DeepSeek connection failed: {type(exc.reason).__name__}"
            ) from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeepSeekResponseError("DeepSeek returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise DeepSeekResponseError("DeepSeek response must be an object")
        return value


class DeepSeekClient:
    def __init__(
        self,
        config: DeepSeekConfig,
        transport: Optional[JsonTransportPort] = None,
        telemetry_listener: Optional[DeepSeekTelemetryListener] = None,
    ) -> None:
        self.config = config
        self._transport = transport or UrllibJsonTransport()
        self.telemetry_listener = telemetry_listener

    def list_models(self) -> list[str]:
        """列出当前密钥实际可用的模型，不打印或返回密钥。"""

        response = self._transport.get_json(
            f"{self.config.base_url}/models",
            {"Authorization": f"Bearer {self.config.api_key}"},
            self.config.timeout_s,
        )
        data = response.get("data")
        if not isinstance(data, list):
            raise DeepSeekResponseError("DeepSeek models response has no data list")
        models = [
            item.get("id")
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        if not models:
            raise DeepSeekResponseError("DeepSeek models response is empty")
        return models

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        timeout_s: Optional[float] = None,
        thinking: bool = False,
        operation: str = "chat_json",
    ) -> dict[str, Any]:
        if not messages or any(
            item.get("role") not in {"system", "user", "assistant"}
            or not isinstance(item.get("content"), str)
            for item in messages
        ):
            raise ValueError("messages must contain valid role/content pairs")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "temperature": 0,
            "max_tokens": self.config.max_tokens,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if thinking:
            payload["reasoning_effort"] = "high"
        started = time.perf_counter()
        response: Optional[dict[str, Any]] = None
        try:
            response = self._transport.post_json(
                f"{self.config.base_url}/chat/completions",
                {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.config.api_key}",
                },
                payload,
                timeout_s if timeout_s is not None else self.config.timeout_s,
            )
            content = _extract_message_content(response)
            value = json.loads(content)
            if not isinstance(value, dict):
                raise DeepSeekResponseError(
                    "DeepSeek JSON output must be an object"
                )
        except json.JSONDecodeError as exc:
            self._emit_telemetry(
                operation, thinking, started, response, "error", type(exc).__name__
            )
            raise DeepSeekResponseError(
                "DeepSeek message content is not a JSON object"
            ) from exc
        except Exception as exc:
            self._emit_telemetry(
                operation, thinking, started, response, "error", type(exc).__name__
            )
            raise
        self._emit_telemetry(
            operation, thinking, started, response, "ok", None
        )
        return value

    def _emit_telemetry(
        self,
        operation: str,
        thinking: bool,
        started: float,
        response: Optional[dict[str, Any]],
        status: str,
        error_type: Optional[str],
    ) -> None:
        listener = self.telemetry_listener
        if listener is None:
            return
        usage = (
            response.get("usage", {})
            if isinstance(response, dict)
            and isinstance(response.get("usage"), dict)
            else {}
        )
        completion_details = usage.get("completion_tokens_details", {})
        if not isinstance(completion_details, dict):
            completion_details = {}
        payload = {
            "operation": operation,
            "model": self.config.model,
            "status": status,
            "error_type": error_type,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "input_tokens": _optional_int(usage.get("prompt_tokens")),
            "output_tokens": _optional_int(usage.get("completion_tokens")),
            "total_tokens": _optional_int(usage.get("total_tokens")),
            "cache_hit_tokens": _optional_int(
                usage.get("prompt_cache_hit_tokens")
            ),
            "cache_miss_tokens": _optional_int(
                usage.get("prompt_cache_miss_tokens")
            ),
            "reasoning_tokens": _optional_int(
                completion_details.get("reasoning_tokens")
            ),
            "thinking": thinking,
            "request_id": (
                response.get("id") if isinstance(response, dict) else None
            ),
        }
        try:
            listener(payload)
        except Exception:
            return


class DeepSeekIntentInterpreter:
    """用户一句话 → 结构化任务草案。task_id 仍由确定性代码生成。"""

    def __init__(self, client: DeepSeekClient) -> None:
        self.client = client

    def interpret(self, raw_input: str, sequence: int) -> dict:
        if not isinstance(raw_input, str) or not raw_input.strip():
            raise ValueError("raw_input is required")
        value = self.client.chat_json(
            [
                {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": raw_input},
            ],
            thinking=False,
            operation="intent_interpretation",
        )
        task_type = _required_string(value, "task_type")
        goal = _required_string(value, "goal")
        success_condition = _required_string(value, "success_condition")
        priority = _enum_string(value, "priority", {"low", "normal", "high", "urgent"})
        capabilities = _string_list(value, "required_capability_ids")
        role_slots = _validated_role_slots(value.get("role_slots"))
        area = value.get("area")
        if not isinstance(area, dict):
            raise DeepSeekResponseError("intent area must be an object")
        area_label = _required_string(area, "label")
        area_name = str(area.get("area") or area_label)
        return {
            "task_id": f"task_{sequence:03d}",
            "task_type": task_type,
            "goal": goal,
            "success_condition": success_condition,
            "priority": priority,
            "required_capability_ids": capabilities,
            "role_slots": role_slots,
            "area": {"label": area_label, "area": area_name},
        }


class DeepSeekLocalProposalPolicy:
    """单设备大模型只补充协作建议，不得改写硬 BID。"""

    def __init__(self, client: DeepSeekClient) -> None:
        self.client = client

    def propose(
        self, context: dict, timeout_s: float
    ) -> Optional[CollaborationProposal]:
        context_fingerprint = _required_string(context, "context_fingerprint")
        accepted_slots = {
            str(item.get("slot_id"))
            for item in context.get("hard_offers", [])
            if isinstance(item, dict) and item.get("decision") == "accept"
        }
        if not accepted_slots:
            return None
        value = self.client.chat_json(
            [
                {"role": "system", "content": _LOCAL_PROPOSAL_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False, sort_keys=True),
                },
            ],
            timeout_s=timeout_s,
            thinking=False,
            operation=(
                "local_proposal:"
                f"{context.get('self_snapshot', {}).get('device_id', 'unknown')}"
            ),
        )
        preferred = [
            item
            for item in _string_list(value, "preferred_slot_ids")
            if item in accepted_slots
        ]
        confidence = _confidence(value.get("confidence"))
        return CollaborationProposal(
            preferred_slot_ids=list(dict.fromkeys(preferred)),
            cooperation_summary=_required_string(value, "cooperation_summary"),
            risk_notes=_optional_string_list(value, "risk_notes"),
            evidence_refs=_optional_string_list(value, "evidence_refs"),
            confidence=confidence,
            context_fingerprint=context_fingerprint,
        )


class DeepSeekGroupPlanningPolicy:
    """群体模型输出候选分配；Coordinator 的确定性 Validator 决定是否采用。"""

    def __init__(self, client: DeepSeekClient, *, thinking: bool = False) -> None:
        self.client = client
        self.thinking = thinking

    def plan(
        self, planning_input: GroupPlanningInput, timeout_s: float
    ) -> CandidateAssignmentPlan:
        value = self.client.chat_json(
            [
                {"role": "system", "content": _GROUP_PLAN_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        to_json_value(planning_input),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ],
            timeout_s=timeout_s,
            thinking=self.thinking,
            operation="group_planning",
        )
        raw_assignments = value.get("assignments")
        if not isinstance(raw_assignments, list):
            raise DeepSeekResponseError("group plan assignments must be a list")
        assignments: list[CandidateAssignment] = []
        for item in raw_assignments:
            if not isinstance(item, dict):
                raise DeepSeekResponseError("group plan assignment must be an object")
            assignments.append(
                CandidateAssignment(
                    slot_id=_required_string(item, "slot_id"),
                    device_id=_required_string(item, "device_id"),
                    supporting_bid_event_id=_required_string(
                        item, "supporting_bid_event_id"
                    ),
                )
            )
        return CandidateAssignmentPlan(
            task_id=str(planning_input.task.get("task_id", "")),
            task_revision=planning_input.task_revision,
            coordination_epoch=planning_input.coordination_epoch,
            assignments=assignments,
            rationale_summary=_required_string(value, "rationale_summary"),
            input_fingerprint=planning_input.input_fingerprint,
        )


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise DeepSeekConfigurationError(
                f"invalid env entry at {path.name}:{line_number}"
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _extract_message_content(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise DeepSeekResponseError("DeepSeek response has no choices")
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise DeepSeekResponseError("DeepSeek response has no message content")
    return content


def _safe_remote_message(body: str) -> str:
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        return "remote request rejected"
    if not isinstance(value, dict):
        return "remote request rejected"
    error = value.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"][:500]
    if isinstance(error, str):
        return error[:500]
    for field_name in ("message", "detail", "msg"):
        message = value.get(field_name)
        if isinstance(message, str):
            return message[:500]
    return "remote request rejected"


def _required_string(value: Mapping[str, Any], field_name: str) -> str:
    item = value.get(field_name)
    if not isinstance(item, str) or not item.strip():
        raise DeepSeekResponseError(f"LLM output requires {field_name}")
    return item.strip()


def _enum_string(
    value: Mapping[str, Any], field_name: str, allowed: set[str]
) -> str:
    item = _required_string(value, field_name)
    if item not in allowed:
        raise DeepSeekResponseError(f"LLM output {field_name} is unsupported")
    return item


def _string_list(value: Mapping[str, Any], field_name: str) -> list[str]:
    items = value.get(field_name)
    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
        raise DeepSeekResponseError(f"LLM output {field_name} must be string[]")
    return [item for item in items if item]


def _optional_string_list(value: Mapping[str, Any], field_name: str) -> list[str]:
    """Normalize optional model notes while preserving the string[] contract."""

    items = value.get(field_name)
    if items is None:
        return []
    if isinstance(items, str):
        item = items.strip()
        return [item] if item else []
    return _string_list(value, field_name)


def _confidence(value: Any) -> float:
    if type(value) not in (int, float):
        raise DeepSeekResponseError("LLM output confidence must be numeric")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise DeepSeekResponseError("LLM output confidence must be within 0..1")
    return result


def _optional_int(value: Any) -> Optional[int]:
    if type(value) is not int:
        return None
    return max(0, value)


def _validated_role_slots(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise DeepSeekResponseError("intent role_slots must be a non-empty list")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise DeepSeekResponseError("intent role slot must be an object")
        slot_id = _required_string(item, "slot_id")
        capabilities = _string_list(item, "required_capability_ids")
        actions = _string_list(item, "allowed_actions")
        if not capabilities or not actions:
            raise DeepSeekResponseError("intent role slot capabilities/actions are required")
        action_template = item.get("action_template")
        completion_rule = item.get("completion_rule")
        if not isinstance(action_template, dict) or not isinstance(completion_rule, dict):
            raise DeepSeekResponseError("intent role slot templates must be objects")
        result.append(
            {
                "slot_id": slot_id,
                "required_capability_ids": capabilities,
                "exclusive": bool(item.get("exclusive", True)),
                "allowed_actions": actions,
                "action_template": action_template,
                "completion_rule": completion_rule,
            }
        )
    return result


_INTENT_SYSTEM_PROMPT = """你是 SwarmBrain 入口层的任务意图解释器。
只输出一个 JSON object，不输出解释。不要生成 task_id。
必须输出字段：task_type, goal, success_condition, priority,
required_capability_ids, area, role_slots。
priority 只能是 low/normal/high/urgent。
机器狗找目标的能力使用 search，动作使用 move_to。
role_slots 每项必须包含 slot_id, required_capability_ids, exclusive,
allowed_actions, action_template, completion_rule。
示例输入：帮我找公园里走失的白色萨摩耶。
示例 JSON：
{"task_type":"search_target","goal":"在公园寻找走失的白色萨摩耶",
"success_condition":"目标被近距离确认","priority":"high",
"required_capability_ids":["search"],"area":{"label":"公园","area":"公园"},
"role_slots":[{"slot_id":"searcher","required_capability_ids":["search"],
"exclusive":true,"allowed_actions":["move_to"],
"action_template":{"verb":"move_to","params":{"target":"search-area"},"reversible":true},
"completion_rule":{"kind":"receipt_success","required_result_fields":[]}}]}"""


_LOCAL_PROPOSAL_SYSTEM_PROMPT = """你是单台机器狗的协作判断模型。
输入是 JSON context。hard_offers 是确定性规则结果，不得修改，不得把 reject 变成 accept。
只输出 JSON object：preferred_slot_ids, cooperation_summary, risk_notes,
evidence_refs, confidence。preferred_slot_ids 只能选择 decision=accept 的槽位。
preferred_slot_ids、risk_notes、evidence_refs 必须是字符串数组，不能是 null 或对象；
没有风险或证据引用时必须输出 []。
格式示例：{"preferred_slot_ids":["searcher"],"cooperation_summary":"可承担搜索",
"risk_notes":[],"evidence_refs":[],"confidence":0.85}
不要输出动作指令、工具调用或额外字段。"""


_GROUP_PLAN_SYSTEM_PROMPT = """你是群体协作候选方案模型。
输入是 JSON GroupPlanningInput。只输出 JSON object：assignments 和 rationale_summary。
assignments 每项只能包含 slot_id, device_id, supporting_bid_event_id；只能使用输入中真实存在且
decision=accept 的 BID，必须覆盖所有角色槽，不得伪造设备、能力、BID 或事件 ID。
你的输出只是候选方案，之后还会经过确定性规则校验。"""
