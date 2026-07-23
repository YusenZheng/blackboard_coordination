# LLM 配置

这里集中存放项目中所有大模型调用共享的配置和 HTTP 调用入口，不依赖任何厂商
SDK。调用接口采用 OpenAI-compatible 的 `/chat/completions` 格式。

默认从以下环境变量读取：

```text
SWARM_BRAIN_LLM_PROVIDER
SWARM_BRAIN_LLM_MODEL
SWARM_BRAIN_LLM_API_KEY
SWARM_BRAIN_LLM_BASE_URL
SWARM_BRAIN_LLM_TEMPERATURE
SWARM_BRAIN_LLM_MAX_TOKENS
SWARM_BRAIN_LLM_TIMEOUT_SECONDS
```

使用方式：

```python
from swarm_brain.llm import LLMConfig, call_llm

config = LLMConfig.from_env()
response = call_llm(
    config,
    [
        {"role": "system", "content": "你是机器人集群的任务规划助手。"},
        {"role": "user", "content": "规划搜索区域。"},
    ],
)
text = response["choices"][0]["message"]["content"]
```

不同用途需要不同模型时，可以使用独立前缀：

```python
planner_config = LLMConfig.from_env(prefix="SWARM_BRAIN_PLANNER_LLM_")
agent_config = LLMConfig.from_env(prefix="SWARM_BRAIN_AGENT_LLM_")
```

API Key 只应通过环境变量或后续接入的密钥管理服务提供，不要写入源码、示例文件或
日志。`safe_summary()` 可用于输出不含密钥的诊断信息。

`SWARM_BRAIN_LLM_BASE_URL` 应填写到 API 版本根路径，例如兼容服务的
`https://example.com/v1`；调用函数会在其后追加 `/chat/completions`。需要
`response_format`、`tools` 等字段时，通过 `request_overrides` 传入。

仓库本地提供了一个不会提交到 Git 的 `.env.local` 测试文件。它默认指向
`127.0.0.1:8000`，使用前请按实际本地模型服务修改。
