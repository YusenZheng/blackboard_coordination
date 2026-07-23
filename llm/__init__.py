"""LLM 配置入口。

本模块只负责集中管理模型配置，不依赖具体厂商 SDK。真正的模型客户端由后续运行时
装配代码按需创建。
"""

from .call import LLMCallError, call_llm
from .config import DEFAULT_ENV_PREFIX, LLMConfig

__all__ = ["DEFAULT_ENV_PREFIX", "LLMCallError", "LLMConfig", "call_llm"]
