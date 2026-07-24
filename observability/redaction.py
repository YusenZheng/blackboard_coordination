"""在进入 SQLite 或应急日志前执行确定性脱敏和 payload 策略。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4


REDACTED = "[REDACTED]"
OMITTED_REASONING = "[OMITTED_HIDDEN_REASONING]"

_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|authorization|proxy[_-]?authorization|cookie|set[_-]?cookie|"
    r"password|passwd|passphrase|client[_-]?secret|access[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|token|secret|credential)",
    re.IGNORECASE,
)
_HIDDEN_REASONING_KEY = re.compile(
    r"(?:chain[_-]?of[_-]?thought|hidden[_-]?reasoning|reasoning[_-]?content|"
    r"internal[_-]?thoughts?|private[_-]?reasoning|scratchpad|deliberation|"
    r"^reasoning$|^cot$)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_API_TOKEN = re.compile(r"(?i)\b(?:sk|pk)-[A-Za-z0-9._-]{8,}")
_NAMED_SECRET = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|password|passwd|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|token|secret)\b"
    r"(\s*[:=]\s*)([^\s,;&}\]\"']+|\"[^\"]*\"|'[^']*')"
)
_URL_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|password|secret)=)"
    r"([^&#\s]+)"
)
_ENV_SECRET = re.compile(
    r"(?im)^([A-Z][A-Z0-9_]*(?:KEY|TOKEN|PASSWORD|PASSWD|SECRET|COOKIE)"
    r"\s*=\s*)([^\r\n]*)$"
)
_BINARY_DATA_URI = re.compile(
    r"(?is)^data:(?:image|audio|video|application/octet-stream)/?[^,]*,"
)
_ENV_CONTAINER_KEYS = {
    ".env",
    "env",
    "dotenv",
    "env_file",
    "environment_variables",
}
_SAFE_TOKEN_COUNT_KEYS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "reasoning_tokens",
}
_SAFE_TOKEN_KIND_VALUES = _SAFE_TOKEN_COUNT_KEYS


def _is_safe_token_telemetry(key: str, value: Any) -> bool:
    """Allow numeric LLM usage telemetry without weakening secret redaction."""

    normalized = key.strip().lower().replace("-", "_")
    if normalized in _SAFE_TOKEN_COUNT_KEYS:
        return (
            value is None
            or (
                type(value) in (int, float)
                and value >= 0
            )
        )
    if normalized == "token.kind":
        return isinstance(value, str) and value in _SAFE_TOKEN_KIND_VALUES
    return False


def redact_text(value: str) -> str:
    """清理字符串中的 header、URL、环境变量及常见 API token。"""

    if _BINARY_DATA_URI.match(value):
        return "[BINARY_DATA_URI_OMITTED]"
    result = _BEARER.sub("Bearer " + REDACTED, value)
    result = _API_TOKEN.sub(REDACTED, result)
    result = _NAMED_SECRET.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", result
    )
    result = _URL_SECRET.sub(lambda match: match.group(1) + REDACTED, result)
    result = _ENV_SECRET.sub(lambda match: match.group(1) + REDACTED, result)
    return result


def redact_value(
    value: Any,
    key: str | None = None,
    _active_ids: set[int] | None = None,
) -> Any:
    """递归脱敏；二进制正文只留下类型、大小和哈希。"""

    active_ids = _active_ids if _active_ids is not None else set()
    if key and key.strip().lower().replace("-", "_") in _ENV_CONTAINER_KEYS:
        return REDACTED
    if key and _HIDDEN_REASONING_KEY.search(str(key)):
        return OMITTED_REASONING
    if key and _is_safe_token_telemetry(str(key), value):
        return value
    if key and _SECRET_KEY.search(str(key)):
        return REDACTED
    if isinstance(value, bytes):
        return {
            "binary": True,
            "media_type": "application/octet-stream",
            "byte_size": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, bytearray):
        raw = bytes(value)
        return {
            "binary": True,
            "media_type": "application/octet-stream",
            "byte_size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_ids:
            return "[CYCLE]"
        active_ids.add(identity)
        try:
            return {
                str(item_key): redact_value(
                    item_value, str(item_key), active_ids
                )
                for item_key, item_value in value.items()
            }
        finally:
            active_ids.discard(identity)
    if isinstance(value, (list, tuple, set, frozenset)):
        identity = id(value)
        if identity in active_ids:
            return "[CYCLE]"
        active_ids.add(identity)
        try:
            return [redact_value(item, _active_ids=active_ids) for item in value]
        finally:
            active_ids.discard(identity)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        return redact_text(str(value))
    except Exception:
        return f"<{type(value).__name__}>"


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return repr(value).encode("utf-8", errors="replace")


def _schema(value: Any, depth: int = 0) -> dict[str, Any]:
    if depth >= 3:
        return {"type": type(value).__name__}
    if isinstance(value, Mapping):
        return {
            "type": "object",
            "fields": {
                str(key): _schema(item, depth + 1)
                for key, item in list(value.items())[:100]
            },
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        types = sorted({type(item).__name__ for item in items[:100]})
        return {"type": "array", "item_types": types}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "binary"}
    if value is None:
        return {"type": "null"}
    return {"type": type(value).__name__}


def _utc_iso_after(days: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(days=days)
    ).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class PayloadProcessor:
    """把 payload 转换成可安全落盘的行。

    ``errors`` 模式由调用方在已知错误时传入 ``is_error=True``；正常路径返回
    ``None``。``full`` 模式也只保存脱敏后的 JSON，且有硬大小上限。
    """

    mode: str = "metadata"
    max_full_bytes: int = 256 * 1024
    summary_chars: int = 256

    def __post_init__(self) -> None:
        if self.mode not in {"metadata", "errors", "full"}:
            raise ValueError("payload mode must be metadata, errors or full")
        if self.max_full_bytes < 1024:
            raise ValueError("max_full_bytes must be at least 1024")

    def prepare(
        self,
        payload: Any,
        *,
        direction: str,
        is_error: bool = False,
        capture_mode: str | None = None,
    ) -> dict[str, Any] | None:
        mode = capture_mode or self.mode
        if mode not in {"metadata", "errors", "full"}:
            mode = self.mode
        if mode == "errors" and not is_error:
            return None

        raw_bytes = _json_bytes(payload)
        safe_value = redact_value(payload)
        safe_bytes = _json_bytes(safe_value)
        safe_text = safe_bytes.decode("utf-8", errors="replace")
        summary = safe_text[: self.summary_chars]
        content: str | None = None
        truncated = False
        effective_mode = mode
        if mode in {"full", "errors"}:
            if len(safe_bytes) <= self.max_full_bytes:
                content = safe_text
            else:
                truncated = True
                # JSON escaping can expand control characters up to sixfold.
                budget = max(64, self.max_full_bytes // 8)
                preview = safe_bytes[:budget].decode("utf-8", errors="ignore")
                content = json.dumps(
                    {"_truncated": True, "preview": preview},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                while (
                    len(content.encode("utf-8")) > self.max_full_bytes
                    and preview
                ):
                    preview = preview[: max(1, len(preview) * 3 // 4)]
                    content = json.dumps(
                        {"_truncated": True, "preview": preview},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )

        return {
            "payload_id": str(uuid4()),
            "direction": direction,
            "capture_mode": effective_mode,
            "content_type": _schema(payload).get("type", "unknown"),
            "schema_json": _schema(payload),
            "byte_size": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "summary": summary,
            "redacted_content_json": content,
            "is_truncated": int(truncated),
            "content_available": int(content is not None),
            "expires_at": _utc_iso_after(
                7 if content is not None else 30
            ),
        }
