"""大模型 HTTP 调用入口。"""

from __future__ import annotations

import json
from typing import Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import LLMConfig


class LLMCallError(RuntimeError):
    """模型请求失败或返回了无法解析的响应。"""


def call_llm(
    config: LLMConfig,
    messages: Sequence[Mapping[str, object]],
    request_overrides: Optional[Mapping[str, object]] = None,
) -> dict:
    """调用 OpenAI-compatible ``/chat/completions`` 接口。

    返回服务端的原始 JSON 对象，由业务调用方按需读取文本、工具调用和 token 用量。
    ``request_overrides`` 可传入 ``response_format``、``tools`` 等额外请求字段。
    """

    config.require_configured()
    if not config.base_url:
        raise LLMCallError("LLM base_url is required for model calls")
    if not messages:
        raise LLMCallError("messages must not be empty")

    payload: dict[str, object] = {
        "model": config.model,
        "messages": [dict(message) for message in messages],
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    if request_overrides:
        payload.update(request_overrides)

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    endpoint = f"{config.base_url.rstrip('/')}/chat/completions"
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            body = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LLMCallError(
            f"LLM request failed with HTTP {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise LLMCallError(f"LLM request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMCallError("LLM request timed out") from exc

    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LLMCallError("LLM response is not valid JSON") from exc

    if not isinstance(result, dict):
        raise LLMCallError("LLM response must be a JSON object")
    return result
