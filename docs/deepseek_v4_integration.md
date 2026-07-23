# DeepSeek V4 接入说明

## 1. 统一配置

- OpenAI-compatible Base URL：`https://api.deepseek.com`
- 默认模型：`deepseek-v4-flash`
- 可选模型：`deepseek-v4-pro`
- 输出模式：JSON object
- 配置入口：`swarm_brain.llm.LLMConfig`
- 调用入口：`swarm_brain.llm.call_llm`
- 环境变量：统一使用 `SWARM_BRAIN_LLM_*`

`llm/.env.local` 已被 Git 忽略，但 Python 代码不会自动读取它。运行前由 Shell
临时注入当前进程；不得把密钥写入源码、黑板事件、Session、Outbox 或日志。

## 2. 本地配置

在启动 Python 前，由当前 Shell 临时设置统一环境变量：

```powershell
$env:SWARM_BRAIN_LLM_PROVIDER = "deepseek"
$env:SWARM_BRAIN_LLM_MODEL = "deepseek-v4-flash"
$env:SWARM_BRAIN_LLM_API_KEY = "<your-key>"
$env:SWARM_BRAIN_LLM_BASE_URL = "https://api.deepseek.com"
$env:SWARM_BRAIN_LLM_TIMEOUT_SECONDS = "60"
```

切换 Pro：

```powershell
$env:SWARM_BRAIN_LLM_MODEL = "deepseek-v4-pro"
```

## 3. 完整协同闭环

正式入口一次运行完整 coordination v2 链路：

```powershell
python -m swarm_brain.runtime.skeleton "帮我找公园里走失的白色萨摩耶"
```

成功结果满足：

- `status = ok`
- `task.status = done`
- `coordination.local_model_errors` 均为 `null`
- `coordination.group_model_error = null`
- `coordination.group_candidate_matches_committed = true`
- `completion.terminal_event_type = task_done`
- Blackboard 事件水位为 9

事件顺序固定为：

```text
TASK_POSTED → BID_ROUND_OPENED → BID × 2 → TASK_ASSIGNED
→ ACTION_INTENT → RECEIPT → ASSIGNMENT_COMPLETED → TASK_DONE
```

该入口使用真实 DeepSeek、真实 coordination v2 和进程内 Blackboard；物理网关
仍为 Mock。输出中的 `runtime.process_mode` 会明确标记
`single_process_in_memory`，不将其描述为多进程或真机跑通。

## 4. 本地实时看板

```powershell
python -m swarm_brain.runtime.dashboard_server
```

浏览器访问 <http://127.0.0.1:8765>。输入任务后，页面通过 Server-Sent Events
订阅正式 `CoordinationRuntime` 的阶段状态和 Blackboard append 事件。DeepSeek
密钥始终只存在于 Python 服务端；服务默认仅监听 localhost。

页面下方的 LLM 调用明细来自统一 `call_llm` 返回的 `/chat/completions` `usage`：
`prompt_tokens`、`completion_tokens`、`total_tokens`；耗时由服务端围绕真实 HTTP
请求使用单调时钟统计。当前闭环通常包含意图识别、两台设备的本地协作判断和群体
集中判给四次调用。

## 5. 装配关系

```python
from swarm_brain.ingress.task_gen import TaskGen
from swarm_brain.llm import LLMConfig
from swarm_brain.runtime.deepseek import (
    DeepSeekClient,
    DeepSeekGroupPlanningPolicy,
    DeepSeekIntentInterpreter,
    DeepSeekLocalProposalPolicy,
)

client = DeepSeekClient(LLMConfig.from_env())

task_gen = TaskGen(DeepSeekIntentInterpreter(client))
local_policy = DeepSeekLocalProposalPolicy(client)
group_policy = DeepSeekGroupPlanningPolicy(client)
```

- `task_gen`：用户一句话转成结构化任务草案。
- `local_policy`：每台机器狗独立调用，产生协作提案，不修改硬 BID。
- `group_policy`：Coordinator 调用，产生候选分配方案。
- 最终判给仍由确定性 `MinimalPlanValidator` 校验；模型失败时回退到确定性匹配。

当前正式入口为单进程内的两个独立 AgentProcessHost；每个 Host 使用独立 Client、
模型上下文、Session、消费 offset 和工作目录。后续切换多进程时需要先将 Blackboard
替换为可跨进程访问的权威服务，再由每个 AgentProcess 在自己的进程内构造客户端。

官方参考：

- <https://api-docs.deepseek.com/guides/function_calling/>
- <https://api-docs.deepseek.com/guides/json_mode/>
- <https://api-docs.deepseek.com/api/list-models>
