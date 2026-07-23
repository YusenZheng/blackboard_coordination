# swarm_brain

面向异构机器人集群(无人机 / 无人车 / 机器狗)的**实体群体 Agent 运行底座**。每台设备抽象为一个云端 Agent 成员,围绕一块共享黑板自组织协作完成真实任务。

本仓库是**骨架**:六层结构全部铺开,实现走最小可运行(mock / 占位跑通最细链路),各层待填实处以 `# STATUS:` 和 `TODO` 标注。系统架构说明见 `[docs/01_总架构图.md](docs/01_总架构图.md)`。

---

## 环境要求

- Python ≥ 3.10
- 核心链路**零第三方依赖**(纯标准库),克隆后直接运行
- 前台接口为可选依赖:`pip install fastapi uvicorn`

### 本地开发环境（Windows / PowerShell 7）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install pytest
```

`.venv/` 仅供本地使用，已加入 `.gitignore`，不会提交到 Git。

## 快速开始

```bash
cd swarm_brain

# 1. 运行 walking skeleton —— 四条核心链路(推荐从这里入门)
python -m swarm_brain.runtime.skeleton

# 2. 跑评测基线用例,输出一次基线报告
python -m eval.runner

# 3. 冒烟测试
pytest tests/
```

三条命令均可直接运行。`skeleton` 是理解整个系统最快的入口——它把六层串起来跑一遍,打印每一步在做什么。

### 可选:前台接口(HTTP)

```bash
pip install fastapi uvicorn
```

```python
import uvicorn
from swarm_brain.runtime.harness import Harness
from api.app import create_app

uvicorn.run(create_app(Harness()), port=8000)
# GET  /tasks  /devices  /assets/skills  /ledger  /stream
# POST /tasks(下任务)  /devices/register(注册设备)  /authorize/{id}  /estop
```

---



## 目录结构

```
swarm_brain/
  contracts/    数据契约 + 接口契约。零依赖,被所有层引用。
  blackboard/   群智空间层:append-only 事件流 + 派生视图 + 四账本 + 租约。
  coordination/ 协同层:Agent Loop + 挡位选择器 + 冲突分治 + 上下文装配。
  assets/       资产层:Trace + Skill(七元组)+ SkillGraph + 自进化 + 运营账本。
  safety/       安全平面(横切):三层门控 + 信任等级 A0–A4 + 可逆性 R0/R1/R2 + 急停。
  access/       接入层:Agent 化注册 + 工具网关 + 适配器 + 遥测 + 坐标。
  ingress/      北向入口:任务生成流水线 + 事件接入 + 三出向接口。
  llm/          大模型配置:连接信息 + 生成参数(不绑定具体厂商 SDK)。
  memory/       记忆:私有记忆 + 共享事实库(接口已定义,实现待补)。
  runtime/      Harness 装配 + walking skeleton。
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
| 资产层    | `assets/`             | Trace / Skill / SkillGraph / evolution / ledger                  |
| 安全平面   | `safety/`             | guardrail / autonomy / reversibility / estop                     |
| 接入层    | `access/`             | registry / tool_gateway / tools/ / adapters / telemetry / frames |
| 北向入口   | `ingress/`            | task_gen(三段流水线)/ event_ingress / interfaces_out                  |
| 大模型配置  | `llm/`                | 统一管理模型连接信息与生成参数，不保存密钥、不绑定具体厂商 SDK                         |
| 记忆     | `memory/`             | 私有记忆 + 事实库(接口位)                                                  |
| 运行时    | `runtime/`            | harness(装配)/ skeleton                                            |
| 独立顶层   | `sim/` `eval/` `api/` | 仿真 / 评测 / 前台接口                                                   |


每个源文件顶部的 `# STATUS:` 标注实现状态:

- **STAGED** —— 骨架已跑通,本迭代要填实的模块。
- **NOT-STAGED** —— 接口已定义、本迭代不实现的模块(如自进化闭环、真机适配器、反向事件流)。

---



## walking skeleton 演示什么

`python -m swarm_brain.runtime.skeleton` 依次跑四条链路:

1. **主链路**:下任务 → 各设备出价 → 招投标判给 → 安全校验 → Agent Loop 自行调用工具 → 回执 → Trace 落档。
2. **授权点 + 并发租约**:不可逆动作挂起等待人工确认;两设备并发 claim 同一资源,一方获租、一方被拒。
3. **可插拔挡位**:同一任务分别走自主应征与集中式两种协同挡位。
4. **线索重协同**:执行中产生新线索(CLUE),事件驱动各 Agent 就地重新协同。

收尾另演示:遥测旁路、急停旁路(急停中拒绝下发动作、解除后恢复)、Trace 模型级细节。

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

