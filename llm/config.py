"""与具体模型 SDK 解耦的 LLM 配置。

密钥只从运行环境传入，不应写入源码或提交到仓库。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional


DEFAULT_ENV_PREFIX = "SWARM_BRAIN_LLM_"


def _optional_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _read_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = _optional_value(environ.get(name))
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _read_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = _optional_value(environ.get(name))
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class LLMConfig:
    """一组可复用的模型连接与生成参数。

    ``provider`` 和 ``model`` 默认留空，让项目在尚未接入真实模型时仍可正常运行。
    调用方准备创建真实客户端前应调用 :meth:`require_configured`。
    """

    provider: str = ""
    model: str = ""
    api_key: Optional[str] = field(default=None, repr=False)
    base_url: Optional[str] = None
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.temperature < 0:
            raise ValueError("temperature must be greater than or equal to 0")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be greater than 0")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        prefix: str = DEFAULT_ENV_PREFIX,
    ) -> "LLMConfig":
        """从环境变量加载配置。

        ``prefix`` 可用于给不同用途配置独立模型，例如
        ``SWARM_BRAIN_PLANNER_LLM_`` 和 ``SWARM_BRAIN_AGENT_LLM_``。
        """

        source = os.environ if environ is None else environ
        return cls(
            provider=source.get(f"{prefix}PROVIDER", "").strip(),
            model=source.get(f"{prefix}MODEL", "").strip(),
            api_key=_optional_value(source.get(f"{prefix}API_KEY")),
            base_url=_optional_value(source.get(f"{prefix}BASE_URL")),
            temperature=_read_float(
                source, f"{prefix}TEMPERATURE", cls.temperature
            ),
            max_tokens=_read_int(source, f"{prefix}MAX_TOKENS", cls.max_tokens),
            timeout_seconds=_read_float(
                source, f"{prefix}TIMEOUT_SECONDS", cls.timeout_seconds
            ),
        )

    @property
    def is_configured(self) -> bool:
        """是否已提供创建模型客户端所需的基本标识。"""

        return bool(self.provider and self.model)

    def require_configured(self) -> None:
        """在首次真实模型调用前快速暴露缺失配置。"""

        missing = [
            name
            for name, value in (("provider", self.provider), ("model", self.model))
            if not value
        ]
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"LLM configuration is incomplete; missing: {joined}")

    def safe_summary(self) -> dict:
        """返回适合日志记录的配置摘要，不泄露密钥。"""

        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "api_key_configured": self.api_key is not None,
        }
