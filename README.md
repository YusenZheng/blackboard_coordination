# swarm_brain

面向异构机器人集群(无人机 / 无人车 / 机器狗)的**实体群体 Agent 运行底座**。每台设备抽象为一个云端 Agent 成员,围绕一块共享黑板自组织协作完成真实任务。

本仓库是**骨架**:六层结构全部铺开,实现走最小可运行(mock / 占位跑通最细链路),各层待填实处以 `# STATUS:` 和 `TODO` 标注。系统架构说明见 [docs/01_总架构图.md](docs/01_总架构图.md)。

---

## 环境要求

- Python ≥ 3.10
- OpenTelemetry 依赖见 `requirements.txt`（API/SDK 固定为 1.44.0）
- SQLite、HTTP Dashboard 和 Memory 使用 Python 标准库

## 快速开始

```powershell
cd C:\path\to\workspace0724

# 1. 创建并启用虚拟环境（已有 .venv 可跳过）
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. 安装固定依赖
python -m pip install -r requirements.txt

# 3. 安全写入本地 DeepSeek 配置
pwsh -File .\scripts\configure_deepseek.ps1

# 4. 连接检查
python -m swarm_brain.runtime.deepseek_healthcheck
```

真实密钥只应保存在 Git 忽略的 `.env.local`。`.env.example` 仅列出变量名和占位值；
不要把真实 API Key 写入或提交到 `.env.example`。运行时先读取 `.env.local`，再由
同名进程环境变量覆盖。

### 启动一次 V2 链路1

在仓库根目录执行：

```powershell
python -m swarm_brain.runtime --capture-mode metadata "帮我找公园里走失的白色萨摩耶"
```

这条命令会同步执行完整的 V2 链路：

```text
CoordinationRuntime → Coordinator → AgentProcessHost
→ PureAgentLoop → ActionExecutor → ToolRuntime/Adapter
```

成功路径产生九个 Blackboard 业务事件，并把 Trace、结构化日志、指标及记忆资产写入
`runtime_data/`。`--capture-mode` 可选：

- `metadata`：默认，只保存脱敏元数据和安全摘要。
- `errors`：仅异常步骤保存脱敏详情。
- `full`：保存脱敏后的完整载荷，供本地调试使用。

兼容入口也会进入同一条 V2 链路，不再启动旧 Harness：

```powershell
python -m swarm_brain.runtime.skeleton "帮我找公园里走失的白色萨摩耶"
```

### 启动可观测平台

```powershell
python -m swarm_brain.runtime.dashboard_server --host 127.0.0.1 --port 8765
```

然后访问 <http://127.0.0.1:8765>。平台只允许绑定 loopback 地址。点击“开始执行”
会在后台创建一次 V2 运行；也可以查看其他进程写入同一 SQLite 数据库的历史运行。
默认数据文件为：

```text
runtime_data/observability.sqlite3
runtime_data/memory.sqlite3
```

同一端口只应启动一个 Dashboard 进程。停止平台时在启动它的终端按 `Ctrl+C`。

### 启动 Blackboard

当前 Blackboard **不是独立的网络服务或常驻进程**，因此没有单独的
`python -m ...blackboard` 启动命令。每次 V2 运行都会在进程内创建一块新的
Blackboard；执行上述 V2 命令，或在 Dashboard 中点击“开始执行”，即会随任务启动
Blackboard。

Blackboard 是该次运行的协同业务事实源；任务结束后，九个事件的可观测副本保存在
`observability.sqlite3` 供平台回放，但这不等于把 Blackboard 变成跨运行共享数据库。
需要单独验证 Blackboard 契约和视图时执行：

```powershell
python -m unittest swarm_brain.blackboard.tests.test_coordination_bridge -v
```

### 回归测试

```powershell
python -m unittest swarm_brain.runtime.tests.test_observability_integration
```

正式入口依次完成意图识别、双设备竞标、群体判给、受限动作、回执和任务终态。
`python -m swarm_brain.runtime.skeleton "<指令>"` 只是同一 V2 入口的兼容别名。

---



## 目录结构

```
swarm_brain/
  contracts/    数据契约 + 接口契约。零依赖,被所有层引用。
  blackboard/   群智空间层:append-only 事件流 + 派生视图 + 四账本 + 租约。
  coordination/ 协同层:Agent Loop + 挡位选择器 + 冲突分治 + 上下文装配。
  assets/       资产层:Skill(七元组)+ SkillGraph + 自进化 + 运营账本。
  safety/       安全平面(横切):三层门控 + 信任等级 A0–A4 + 可逆性 R0/R1/R2 + 急停。
  access/       接入层:Agent 化注册 + 工具网关 + 适配器 + 遥测 + 坐标。
  ingress/      北向入口:任务生成流水线 + 事件接入 + 三出向接口。
  observability/ OTel Trace + SQLite 日志/指标/载荷 + 脱敏与应急降级。
  memory/       SQLite 私有情节记忆 + 审核制共享事实/Skill 候选。
  runtime/      coordination v2 链路1装配入口 + 本地可观测平台。
sim/            仿真:可通过性引擎 + 仿真适配器 + 网格桩(独立顶层)。
eval/           评测:用例 + 指标 + 基线报告(独立顶层)。
api/            前台接口:FastAPI 端点(可选)。
tests/          冒烟测试。
```


| 层      | 目录                    | 职责                                                               |
| ------ | --------------------- | ---------------------------------------------------------------- |
| 契约(地基) | `contracts/`          | Agent Card / 黑板事件 / 任务包 / 动作意图 / 动词表 / 五层接口 Protocol             |
| 群智空间层  | `blackboard/`         | 事件流 + 派生视图 + 四账本 + 租约                                            |
| 协同层    | `coordination/`       | Agent Loop / 挡位选择(自主应征·集中式)/ 冲突分治 / 上下文装配                        |
| 资产层    | `assets/`             | Skill / SkillGraph / evolution / ledger                          |
| 安全平面   | `safety/`             | guardrail / autonomy / reversibility / estop                     |
| 接入层    | `access/`             | registry / tool_gateway / tools/ / adapters / telemetry / frames |
| 北向入口   | `ingress/`            | task_gen(三段流水线)/ event_ingress / interfaces_out                  |
| 可观测平面 | `observability/`      | OTel Trace / 结构化日志 / 指标 / SQLite / 脱敏 / 保留策略                   |
| 记忆     | `memory/`             | 私有 episodic + 审核制共享事实与 SkillCandidate                           |
| 运行时    | `runtime/`            | CoordinationRuntime / DeepSeek adapters / Dashboard              |
| 独立顶层   | `sim/` `eval/` `api/` | 仿真 / 评测 / 前台接口                                                   |


每个源文件顶部的 `# STATUS:` 标注实现状态:

- **STAGED** —— 骨架已跑通,本迭代要填实的模块。
- **NOT-STAGED** —— 接口已定义、本迭代不实现的模块(如自进化闭环、真机适配器、反向事件流)。

---



## V2 链路1与可观测闭环

当前唯一正式运行链为：

```text
CoordinationRuntime → Coordinator → AgentProcessHost
→ PureAgentLoop → ActionExecutor → ToolRuntime/Adapter
```

一次成功运行仍以 Blackboard 的九个业务事件为权威事实。Trace、模块输入输出、
结构化日志和指标进入独立观测库；任务结束后由确定性 `TaskEpisodeBuilder` 生成
TraceAsset，合格资产自动写入参与 Agent 的私有情节记忆。共享事实与
SkillCandidate 必须在本地 Dashboard 审核后才能使用。

默认载荷模式为 `metadata`；可在调用 `CoordinationRuntime.run(...,
capture_mode="errors"|"full")` 时选择。无论模式如何，凭据、Cookie、Token、
隐藏思维和二进制正文都不会保存。

---



## 跑通分级

对外说明"跑通"时须带级别,避免将 L0 误解为端到端可用:


| 级别               | 含义                                       | 状态  |
| ---------------- | ---------------------------------------- | --- |
| **L0 契约级**       | 全 mock、内存存储。验证契约自洽、依赖不冲突、主循环顺序正确、四链路结构成立 | 已跑通 |
| **L1 真 LLM 小网格** | 接入 10×10 迷雾网格,真 LLM 读黑板自主决策              | 待接入 |
| **L2 仿真闭环**      | 全流程仿真闭环                                  | 规划中 |
| **L3 真机**        | 单机 → 双机 → 窄巷接力真机                         | 规划中 |


- **L0 已验证**:数据契约在单进程内自洽、依赖防火墙不冲突、主循环顺序正确、链路结构成立。
- **L0 未验证**:真 LLM 涌现(当前出价为占位)、并发一致性(内存存储)、物理执行(mock)、云端弱网。

---



## 开发指引



### 添加一个 Tool

在 `access/tools/` 下新建文件,参照 `[g01_navigate.py](swarm_brain/access/tools/g01_navigate.py)`:定义 `ToolSpec`(id / 所属类 / 输入输出 schema / run),注册进 `ToolRegistry`。工具网关 `call_tool` 从注册表查找调用;Agent Loop 在循环内经 `act` 发起工具调用。

### 添加一个 Skill

在 `assets/skills/` 下新建文件,参照 `[fanshaped_search.py](swarm_brain/assets/skills/fanshaped_search.py)`:填好七元组,经 `SkillGraph` 加载。Agent Loop 决策前以 `match_by_situation` 检索命中的 Skill 注入上下文——是否采用由 Loop 自行决定(机制与策略分离)。

### 添加一个协同挡位

`coordination/mode_selector.py` 的 `route()` 按任务标签分流。当前实现自主应征与集中式两挡;新增挡位在此扩展。

### 接入一类设备

在 `access/adapters/` 下参照 `mock_adapter.py` 与 `base.py` 实现适配器,将契约动词翻译为具体协议指令。真机未打通时,`sim/sim_adapter.py` 以仿真角色顶替。

---



## 依赖规则

层间依赖单向,不可逆:

```
contracts   ← 被所有层引用,自身不引用任何层
blackboard  ← 只引用 contracts
access / safety / assets  ← contracts(+ 读写 blackboard)
coordination ← contracts / blackboard / access / safety
ingress     ← contracts / blackboard
sim         ← contracts,经 access/adapters/base 接入各层
eval        ← contracts + 订阅采集,不被业务层引用
runtime     ← 装配所有层(唯一可引用全部层之处)
memory      ← 只被引用,不引用业务层
```

两条铁律:**下层不引用上层;一切协同经黑板**。

## 三条合法旁路

"一切经黑板"有三处例外,均为一等原语(见 `contracts/bypass.py`),是明确的架构设计:


| 旁路       | 原因                                                    |
| -------- | ----------------------------------------------------- |
| **遥测通道** | 高频状态(每秒数十条位姿电量)若走四账本折叠会冲垮黑板;旁路直更 Agent Card,仍抄送 Trace |
| **急停**   | 急停旁路所有软件层直达设备驱动——软件层可故障,急停不能                          |
| **授权点**  | 不可逆动作挂起 Loop 等待人工确认,走人类确认账本寻址                         |




## 契约变更规则

`contracts/` 被全层引用,变更成本最高:

- 新增字段带默认值 = 向后兼容;删除或修改字段 = 破坏性变更,`CONTRACT_VERSION` 递增,评估全部受影响层。
- 枚举新增值 = 兼容;删除或修改值 = 破坏性变更。
- 关键 dataclass 预留 `extra: dict` 开口,便于扩展而不改契约本身。

