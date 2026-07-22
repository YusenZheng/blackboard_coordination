# 黑板模块：基于 `skeleton.py` 的流程、数据流与分阶段实施设计

> 文档状态：实施设计稿  
> 适用代码：`swarm_brain` 当前 L0 walking skeleton  
> 范围：只建设黑板及其必要接缝；暂不建设前端、HTTP API、可视化大屏和真实设备协议

## 1. 文档目标

本文解决四个问题：

1. `runtime/skeleton.py` 的每个流程中，黑板到底负责什么。
2. 每一步进入黑板的数据是什么，黑板输出给谁、输出什么。
3. 当前链路中哪些数据缺失、重复或绕过了黑板。
4. 如何分阶段把黑板从内存演示版建设成可持久化、可重放、可并发消费的可靠模块。

本文不把所有能力一次做完。实施顺序是：

```text
事件契约正确
  → 单进程行为正确
  → 持久化和重放
  → 多进程可靠消费
  → 性能与运维硬化
```

核心原则：

> 黑板是协同事实的唯一来源；状态变化写成事件，当前状态由事件折叠得到。

“唯一来源”不表示所有数据都进入黑板。高频遥测、模型原始推理细节和真实急停控制有自己的旁路，黑板只保存协同所需事实或审计事件。

## 2. 范围与边界

### 2.1 黑板负责

- 接收并校验协同事件。
- 为事件分配稳定的 `offset` 和聚合版本。
- 幂等去重，同一逻辑写入重复提交只生效一次。
- 保存完整、不可修改的事件历史。
- 按任务、事件类型、账本、设备和 offset 查询事件。
- 把事件可靠地交付给 Trace、AgentLoop、投影器等消费者。
- 维护任务、线索、回执、授权、安全和资源租约的当前视图。
- 支持资源租约的原子申请、续租、释放和过期。
- 支持重启后 replay，恢复视图、消费进度、挂起授权和租约。

### 2.2 黑板不负责

- 不理解用户自然语言；这是 `ingress.task_gen` 的职责。
- 不决定哪个 Agent 中标；黑板只保存报价和判给结果。
- 不生成机器人动作；这是 AgentLoop/协调层的职责。
- 不执行设备命令；这是 ToolGateway/Adapter 的职责。
- 不进行安全判断；安全模块判断后把结果留痕到黑板。
- 不保存每秒几十次的高频遥测；遥测直接更新设备状态并进入 Trace。
- 不承担真实急停控制；急停必须旁路软件层，黑板只做审计留痕。
- 本轮不建设前端接口、SSE、WebSocket 或可视化页面。

## 3. 当前代码位置

| 文件 | 当前职责 |
|---|---|
| `contracts/blackboard_event.py` | `Ledger`、`EventType` 和 `BlackboardEvent` 定义 |
| `contracts/interfaces.py` | `BlackboardPort` 接口形状 |
| `blackboard/board.py` | 内存事件流、同步订阅、简单租约 |
| `blackboard/view.py` | 从历史事件折叠当前视图 |
| `runtime/harness.py` | 创建黑板并连接 Trace、AgentLoop、安全和冲突服务 |
| `runtime/skeleton.py` | 当前端到端演示和主要黑板生产者 |
| `coordination/agent_loop.py` | 消费任务/线索事件，生产 BID/RECEIPT |
| `coordination/master.py` | 集中式模式生产 TASK_ASSIGNED |
| `coordination/conflict.py` | 通过黑板申请/释放资源租约 |
| `safety/reversibility.py` | 生产 AUTH_POINT/AUTH_DECISION |
| `safety/estop.py` | 生产 ESTOP 审计事件 |
| `assets/trace.py` | 订阅全部黑板事件并派生归档 |

注意：`blackboard/board2.py` 是通俗注释副本；运行时仍然使用 `blackboard/board.py`。

## 4. 目标黑板模型

黑板由五部分组成：

```text
事件契约 Event Contract
        ↓
事件存储 Event Store ─────→ 查询历史
        ↓
可靠分发 Dispatcher ─────→ Trace / AgentLoop / Projector
        ↓
派生视图 Projections ─────→ 当前任务、线索、回执、授权、安全状态
        ↓
资源租约 Lease Store ─────→ 独占资源与 fencing token
```

### 4.1 四类账本

| 账本 | 保存内容 | `skeleton.py` 中的事件 |
|---|---|---|
| `TASK` | 任务生命周期和任务协同 | TASK_POSTED、BID、TASK_ASSIGNED、TASK_DONE |
| `EVIDENCE` | 线索、观测、融合结果 | CLUE；未来 FUSION |
| `RECEIPT` | 动作结果、资源、安全审计 | RECEIPT、CLAIM、ESTOP |
| `HUMAN` | 人工授权过程 | AUTH_POINT、AUTH_DECISION |

### 4.2 目标事件信封

当前 `content: dict` 保留扩展性，但外层必须增加稳定的路由、版本和因果字段：

```python
@dataclass(frozen=True)
class BlackboardEvent:
    event_id: str
    event_type: EventType
    ledger: Ledger
    aggregate_id: str          # 通常为 task_id；租约事件可为 resource_id
    task_id: str | None         # 跨 task/lease/action 聚合查询的稳定路由字段
    aggregate_version: int     # 同一 aggregate 内递增
    source: str
    payload: Mapping[str, Any]
    occurred_at: datetime
    idempotency_key: str
    schema_version: int = 1
    correlation_id: str | None = None   # 一条任务链路的统一 ID
    causation_id: str | None = None     # 直接触发本事件的事件 ID
    confidence: float | None = None
    expires_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
```

必须保证：

- 写入后事件不可被调用者修改。
- `event_id` 全局唯一。
- `idempotency_key` 对所有会重试的生产者必填。
- `aggregate_version` 通过 `expected_version` 做乐观并发校验。
- `occurred_at` 自动生成 UTC 时间，不能默认为 `0`。
- `expires_at` 只决定事件是否参与当前决策，不删除审计历史。
- `payload` 按 `EventType` 做字段校验，不允许任意缺字段字典进入事件流。

### 4.3 目标接口输入输出

```python
@dataclass(frozen=True)
class AppendResult:
    event_id: str
    offset: int
    aggregate_version: int
    duplicate: bool

@dataclass(frozen=True)
class EventQuery:
    aggregate_id: str | None = None
    task_id: str | None = None
    ledgers: tuple[Ledger, ...] = ()
    types: tuple[EventType, ...] = ()
    source: str | None = None
    since_offset: int = 0
    limit: int = 500

class BlackboardPort(Protocol):
    def append(self, event, expected_version=None) -> AppendResult: ...
    def append_batch(self, events, expected_versions=None) -> list[AppendResult]: ...
    def read_events(self, query: EventQuery) -> list[StoredEvent]: ...
    def query_view(self, view_name: str, key: str | None = None) -> dict: ...
    def subscribe(self, consumer_name, handler, types=None, start='resume') -> str: ...
    def ack(self, consumer_name: str, offset: int) -> None: ...
    def nack(self, consumer_name: str, offset: int, reason: str) -> None: ...
    def unsubscribe(self, subscription_id: str) -> None: ...
```

`append()` 不再只返回裸 `offset`。重复写入时必须返回原事件的 offset，而不是“当前最后一条事件”的 offset。

## 5. `skeleton.py` 全流程中的黑板职责和数据流

### 5.1 流程零：系统装配与订阅

代码位置：`runtime/harness.py:32-70`。

```text
Harness 创建 Blackboard
  ├─ Trace 订阅全部事件
  ├─ dog_a AgentLoop 订阅 TASK_POSTED/CLUE/TASK_ASSIGNED
  └─ dog_b AgentLoop 订阅 TASK_POSTED/CLUE/TASK_ASSIGNED
```

输入：

```text
consumer_name
handler(event, offset)
关心的 EventType
起始策略：latest / from_offset / resume
```

黑板输出：

```text
subscription_id
后续 Delivery(event, offset, attempt)
```

当前问题：

- `subscribe()` 默认从 offset 0 同步补发全部历史。
- 新加入的 Agent 会对已完成旧任务重新报价。
- Harness 丢弃了 `subscription_id`，无法取消订阅。
- 没有 ACK、失败重试和消费者独立进度。

目标规则：

- Trace 使用 `start='from_beginning'`。
- 投影器使用 `start='resume'`。
- 新 Agent 默认使用 `start='latest'`，然后单独查询仍在进行的任务。
- 每个消费者独立保存 `last_acked_offset`。

### 5.2 流程一-A：用户任务生成

代码位置：`runtime/skeleton.py:59-62`。

```python
task = h.task_gen.generate("帮我找走失的萨摩耶幼犬")
```

这一步不属于黑板。输入是用户自然语言，输出是完整 `TaskPackage`。黑板从下一步才开始负责。

### 5.3 流程一-B：发布任务 TASK_POSTED

代码位置：`runtime/skeleton.py:64-70`。

当前输入：

```json
{
  "task_id": "find_dog_001",
  "goal": "帮我找走失的萨摩耶幼犬"
}
```

当前黑板处理：

```text
校验简单幂等键（当前事件未提供）
→ 分配 version 和 offset
→ 追加 TASK_POSTED
→ 同步通知 Trace、dog_a、dog_b
```

当前输出：

- `append()` 返回 offset，但调用方没有保存。
- Trace 保存任务事件。
- 两个 AgentLoop 收到 TASK_POSTED，分别产生 BID。

目标输入必须保存完整任务，而不是只保存 `task_id + goal`：

```json
{
  "task_id": "find_dog_001",
  "task_type": "search_target",
  "goal": "帮我找走失的萨摩耶幼犬",
  "success_condition": "目标被近距离确认",
  "safety_constraints": [],
  "requirement": {
    "required_capabilities": ["G01"],
    "min_battery": 0.2
  },
  "target_profile": {
    "species": "萨摩耶幼犬",
    "color": "白",
    "collar": "红"
  },
  "area": {"label": "公园", "area": "公园"},
  "priority": "high",
  "initial_autonomy_level": "A1",
  "mode": "autonomous"
}
```

目标输出：

```python
AppendResult(
    event_id="post-find_dog_001",
    offset=0,
    aggregate_version=1,
    duplicate=False,
)
```

关键约束：同一任务重试发布必须使用稳定幂等键 `task:{task_id}:posted`。

### 5.4 流程一-C：Agent 应征 BID

代码位置：`coordination/agent_loop.py:41-100`。

输入：

- TASK_POSTED 的完整 payload。
- 本设备 AgentCard、电量和能力。
- 从黑板查询到的该任务当前状态、现有线索和竞价窗口。
- Skill 检索结果。

每台 Agent 的黑板输出：

```json
{
  "event_type": "bid",
  "ledger": "task",
  "aggregate_id": "find_dog_001",
  "source": "dog_a",
  "payload": {
    "task_id": "find_dog_001",
    "device_id": "dog_a",
    "decision": "accept",
    "bid_score": 1.0,
    "reason_codes": ["battery_ok", "capability_match"]
  },
  "idempotency_key": "task:find_dog_001:bid:dog_a"
}
```

黑板输出给消费者：

- Trace 收到 BID。
- 招投标协调器读取当前任务的 BID 集合。
- 任务视图更新 `bid_count`、应征设备和竞价状态。

当前问题：

- Agent 实际只收到 `task_id`，没有完整任务上下文。
- `ContextAssembler` 没有真正读取黑板。
- 同步嵌套 append 会让 dog_b 先收到 dog_a 的 BID，再收到原 TASK_POSTED。

目标实现必须把事件落库与回调分开：先提交 TASK_POSTED，再由独立 dispatcher 按 offset 分发；消费者产生的 BID 在后续 offset 中处理。

### 5.5 流程一-D：收集报价并判给 TASK_ASSIGNED

代码位置：`runtime/skeleton.py:72-91`。

当前输入：

```python
read_events(types=[EventType.BID])
```

当前输出：

```json
{
  "event_type": "task_assigned",
  "payload": {
    "task_id": "find_dog_001",
    "device_id": "dog_a"
  },
  "source": "coord"
}
```

当前问题：

- 读取了所有任务的 BID，没有按 `task_id` 过滤。
- 没有竞价截止时间或“期望设备已经响应”的条件。
- 当前立即读到两条 BID，依赖同步回调；改成异步后可能在 BID 尚未到达时提前判给。
- TASK_ASSIGNED 没有消费者，任务不会因为判给事件自动开始执行。

目标输入查询：

```python
EventQuery(
    aggregate_id="find_dog_001",
    types=(EventType.BID,),
    since_offset=bidding_started_offset,
)
```

目标判给事件：

```json
{
  "task_id": "find_dog_001",
  "device_id": "dog_a",
  "assignment_mode": "autonomous_bid",
  "winning_score": 1.0,
  "considered_bid_event_ids": ["bid-a", "bid-b"]
}
```

状态机必须执行：

```text
POSTED/BIDDING --TASK_ASSIGNED--> ASSIGNED
```

并使用 `expected_version` 防止两个协调器重复判给。

### 5.6 流程一-E：R0 动作执行与 RECEIPT

代码位置：`runtime/skeleton.py:93-112`、`coordination/agent_loop.py:107-124`。

黑板不负责执行 `ActionIntent`，但必须记录最终执行结果。

输入：ToolGateway 返回的完整 `ActionReceipt`。

目标 RECEIPT payload：

```json
{
  "task_id": "find_dog_001",
  "intent_id": "mv-1",
  "device_id": "dog_a",
  "verb": "move_to",
  "success": true,
  "result": {"position": "waypoint_A"},
  "failure_reason": null,
  "recovery_class": null,
  "partial_progress": null,
  "duration_s": 2.4
}
```

黑板输出：

- 回执视图更新 `receipts[intent_id]`。
- Trace 保存回执。
- 失败恢复消费者可根据 `recovery_class` 产生重试、换机或人工介入事件。

当前只保存 `intent_id/device_id/success`，导致黑板无法解释失败和恢复方式。

另外，`skeleton.py:108-110` 直接调用 `call_tool("G01")`，完全没有黑板记录。正式流程必须统一经过 AgentLoop 或一个受控执行服务，确保每次物理动作至少形成：

```text
ACTION_REQUESTED（建议新增）
→ 安全裁决
→ RECEIPT
```

演示性工具探测可以保留，但必须明确它不是业务动作。

### 5.7 流程二-A：R1 授权 AUTH_POINT/AUTH_DECISION

代码位置：`runtime/skeleton.py:117-130`、`safety/reversibility.py:64-97`。

授权请求输入：

```json
{
  "intent_id": "enter-1",
  "task_id": "find_dog_001",
  "device_id": "dog_a",
  "action": "进入绿篱后封闭缝隙",
  "risk_level": "R1",
  "reason": "动作不可逆",
  "intent_snapshot": {}
}
```

黑板输出事件一：

```text
AUTH_POINT，授权视图状态变为 pending
```

人工决定输入：

```json
{
  "intent_id": "enter-1",
  "approved": true,
  "approver": "指挥官",
  "reason": "确认允许进入",
  "decided_at": "UTC timestamp"
}
```

黑板输出事件二：

```text
AUTH_DECISION，授权视图变为 approved/rejected
```

必须保证：

- 未收到 `approved=True` 时不能执行动作。
- 决定写入失败时不能先清除 pending。
- 授权后的执行也必须经统一执行出口并写完整 RECEIPT。
- 重启后可以从 AUTH_POINT 与 AUTH_DECISION 重建仍处于 pending 的动作。

当前 skeleton 直接 `tool_gateway.dispatch(intent2)`，没有把 R1 动作回执写入黑板，而且没有检查 `approved` 后再决定是否执行，必须在阶段一修复。

### 5.8 流程二-B：资源 CLAIM/YIELD 与仲裁

代码位置：`runtime/skeleton.py:132-150`、`coordination/conflict.py:37-55`。

目标 claim 输入：

```python
ClaimRequest(
    resource_id="passage_X",
    holder_id="dog_a",
    task_id="find_dog_001",
    ttl_seconds=30,
)
```

目标原子输出：

```python
LeaseResult(
    acquired=True,
    resource_id="passage_X",
    holder_id="dog_a",
    fencing_token=7,
    expires_at=...,
    event_offset=...,
)
```

黑板必须在同一原子操作内：

```text
检查当前租约
→ 创建/续租/拒绝
→ 增加 fencing_token
→ 追加 CLAIM_GRANTED 或 CLAIM_REJECTED 事件
→ 返回 LeaseResult
```

释放输入：`resource_id + holder_id + fencing_token`。

释放输出：`released: bool + YIELD/LEASE_RELEASED 事件`。

当前问题：

- `_leases` 字典修改和 CLAIM 事件写入是两个操作，可能不一致。
- dog_b 的失败申请没有事件。
- 代码只打印 dog_b “yield”，没有调用 `release()`，也没有 YIELD 事件。
- 仲裁结果没有更新租约，也没有事件。
- 租约没有 TTL、续租、过期或 fencing token，无法安全重放。

### 5.9 流程三：集中式 Master 判给

代码位置：`runtime/skeleton.py:152-161`、`coordination/master.py:25-40`。

当前 Master 直接写 TASK_ASSIGNED，但第二个任务从未写 TASK_POSTED。

这会导致：

- 黑板没有第二个任务的定义。
- 任务视图只有一个无内容的 assigned 空壳。
- Trace/运营账本漏计该任务。
- Agent 不知道任务目标和约束。

目标流程必须统一为：

```text
完整 TASK_POSTED(mode=master_worker)
→ Master 从 Registry 选择设备
→ TASK_ASSIGNED(assignment_mode=master_worker)
```

自主模式和集中模式只允许在“如何产生 TASK_ASSIGNED”上不同，任务发布、执行、回执和完成事件必须共用一套黑板协议。

### 5.10 流程四：CLUE 与重规划

代码位置：`runtime/skeleton.py:163-182`、`coordination/agent_loop.py:41-56`。

目标 CLUE 输入：

```json
{
  "clue_id": "clue-1",
  "task_id": "find_dog_001",
  "position": {"label": "waypoint_B", "area": "片区B"},
  "description": "发现疑似目标",
  "confidence": {
    "level": "mid",
    "score": 0.6,
    "method": "mock"
  },
  "source_device": "dog_a",
  "observed_at": "UTC timestamp",
  "expires_at": "UTC timestamp"
}
```

黑板输出：

- 证据视图保存完整线索、来源、置信度和新鲜度。
- 相关任务的 AgentLoop 收到一次 CLUE delivery。
- 线索融合器可产生 FUSION。
- 重规划协调器可产生 TASK_REPLAN，随后触发 BID/YIELD/HANDOFF/ASK_HELP。

当前存在明确的重复调用：

```text
append(CLUE)
→ subscribe 已经让每个 AgentLoop 调用一次 on_replan
→ skeleton 又调用 h.trigger_replan()
→ 每个 AgentLoop 再调用一次 on_replan
```

目标只保留事件驱动路径，删除手工 `trigger_replan()`；否则真正实现 `on_replan()` 后会双重竞价或双重改派。

当前 CLUE 也丢失 `area`、置信等级和方法；`view.py` 只保存 content，又丢掉 envelope 中的 source、confidence、时间和 TTL。

### 5.11 收尾-A：遥测旁路

代码位置：`runtime/skeleton.py:184-192`。

遥测当前走：

```text
TelemetryChannel → Registry 当前设备状态 + Trace 遥测桶
```

这是合理的黑板边界。高频电量、姿态和位置不应全部变成黑板事件，否则会淹没协同事件。

只有具有协同意义的状态变化才进入黑板，例如：

- `DEVICE_LOW_BATTERY`
- `DEVICE_OFFLINE`
- `DEVICE_STUCK`
- `CAPABILITY_CHANGED`

输入来自遥测聚合器，输出是低频、去抖后的领域事件。

### 5.12 收尾-B：急停、失败回执与解除

代码位置：`runtime/skeleton.py:194-205`、`safety/estop.py:20-43`。

急停控制必须旁路黑板先作用于设备；黑板只接收审计事件：

```json
{
  "event_type": "estop",
  "payload": {
    "scope": "all",
    "state": "activated",
    "reason": "演示:一键急停"
  },
  "source": "estop-bus"
}
```

急停中动作失败后，AgentLoop 应写完整 RECEIPT：

```json
{
  "intent_id": "after-estop",
  "success": false,
  "failure_reason": "device_estopped",
  "recovery_class": "need_human"
}
```

解除急停写入同一事件类型但状态为 `cleared`。安全视图根据事件顺序维护每个 scope 的当前急停状态。

当前 view 不处理 ESTOP，回执也没有保存 failure_reason，因此黑板无法解释“为什么动作失败”。

### 5.13 收尾-C：TASK_DONE、查询视图、Trace 和运营账本

代码位置：`runtime/skeleton.py:207-217`。

目标 TASK_DONE 输入：

```json
{
  "task_id": "find_dog_001",
  "result": "target_confirmed",
  "success_condition_met": true,
  "evidence_ids": ["clue-1"],
  "completed_by": "dog_a",
  "completed_at": "UTC timestamp",
  "summary": {}
}
```

黑板处理：

```text
校验当前任务允许完成
→ 以 expected_version 追加 TASK_DONE
→ 更新任务投影为 done
→ 通知 Trace/AgentLoop/运营投影器
```

黑板输出：

```python
query_view("task", "find_dog_001")
# {
#   "task_id": "find_dog_001",
#   "status": "done",
#   "owner": "dog_a",
#   "version": ...,
#   "result": ...
# }
```

Trace 订阅黑板事件，是派生档案，不应与黑板双写同一协同事实。运营账本再从 Trace 或独立投影消费事件。

当前 TASK_DONE 只有 `task_id`，无法证明成功条件是否满足；`view.py` 也没有折叠 BID、CLAIM、YIELD、ESTOP、TASK_REPLAN 等事件。

## 6. 当前 skeleton 实际留下的事件与缺失项

当前 happy path 大致产生以下事件：

```text
0  TASK_POSTED
1  BID dog_a
2  BID dog_b
3  TASK_ASSIGNED
4  RECEIPT mv-1
5  AUTH_POINT
6  AUTH_DECISION
7  CLAIM dog_a
8  第二任务 TASK_ASSIGNED
9  CLUE
10 ESTOP activated
11 RECEIPT after-estop
12 ESTOP cleared
13 TASK_DONE
```

没有形成黑板事件的业务事实：

- 直接 `call_tool(G01)` 的动作请求和回执。
- R1 授权后动作的执行回执。
- dog_b 的 claim 失败。
- 实际的 YIELD/release。
- 仲裁结果及租约变更。
- 新线索后的重规划结果。
- 集中式第二任务的 TASK_POSTED。

因此第一阶段的目标不是换数据库，而是先把事件语义补完整。

## 7. 目标状态模型

### 7.1 任务状态

```text
POSTED → BIDDING → ASSIGNED → RUNNING → DONE
                          ├→ REPLANNING → ASSIGNED/RUNNING
                          ├→ FAILED
                          └→ CANCELLED
```

建议补充事件：

- `BIDDING_OPENED`
- `BIDDING_CLOSED`
- `TASK_STARTED`
- `TASK_FAILED`
- `TASK_CANCELLED`
- `ACTION_REQUESTED`
- `CLAIM_GRANTED`
- `CLAIM_REJECTED`
- `LEASE_RELEASED`
- `LEASE_EXPIRED`

### 7.2 授权状态

```text
NONE → PENDING → APPROVED
               ├→ REJECTED
               └→ EXPIRED
```

### 7.3 租约状态

```text
AVAILABLE → ACTIVE → RELEASED
                   ├→ EXPIRED
                   └→ REVOKED
```

每次从 AVAILABLE/EXPIRED 获得租约时增加 `fencing_token`。设备执行受保护动作时必须携带该 token，旧持有者即使网络恢复也不能继续使用已经重新分配的资源。

## 8. 目标派生视图

| 视图 | 主键 | 至少包含 |
|---|---|---|
| `task_view` | task_id | 完整任务、status、owner、mode、version、结果 |
| `bid_view` | task_id + device_id | 决定、分数、原因、是否进入判给 |
| `evidence_view` | clue_id | 位置、来源、置信度、TTL、验证/融合状态 |
| `receipt_view` | intent_id | 完整结果、失败原因、恢复分类、耗时 |
| `authorization_view` | intent_id | pending/approved/rejected、审批人、原因 |
| `safety_view` | scope | 急停状态、最后变化时间、原因 |
| `lease_view` | resource_id | holder、task_id、token、expires_at、状态 |
| `consumer_view` | consumer_name | last_acked_offset、lag、last_error |

投影必须是可删除、可从事件流完全重建的派生数据。

## 9. 分阶段实施路线

### 阶段 0：冻结事件契约和不变量

目标：先明确“什么数据才算合法黑板事件”，避免把当前缺失语义直接持久化。

开发内容：

1. 扩展 `BlackboardEvent` 信封：`aggregate_id`、`aggregate_version`、`schema_version`、`correlation_id`、`causation_id`、UTC 时间。
2. 为 skeleton 涉及的每个 EventType 定义强类型 payload。
3. 定义任务、授权和租约状态机。
4. 将 `append()` 返回值升级为 `AppendResult`。
5. `read_events()` 支持 aggregate/task、ledger、type、source、offset、limit 过滤。
6. 所有事件生产者定义稳定的幂等键规则。
7. 明确交付语义为 **at-least-once + 消费者幂等**，不声称端到端 exactly-once。

建议新增文件：

```text
contracts/blackboard_payloads.py
contracts/blackboard_errors.py
blackboard/validation.py
```

验收标准：

- 每个 EventType 都有契约测试。
- 缺必填字段、错误 ledger、非法 confidence、过期 expected_version 会被拒绝。
- 同一 idempotency_key 连续 append 100 次只保存一条，100 次都返回同一 offset。
- TASK_ASSIGNED 不能在 TASK_POSTED 之前发生。

### 阶段 1：完成可靠的单进程内存黑板

目标：不接数据库，先让 `skeleton.py` 的全部黑板语义正确、可测试、可重放。

开发内容：

1. 使用 `threading.RLock` 保护 offset、版本、幂等索引、事件列表和租约。
2. 幂等索引从 `set` 改为 `dict[idempotency_key, AppendResult]`。
3. 事件写入前验证并深复制/冻结，保证 append-only。
4. `append()` 只负责原子提交，不在调用栈内直接执行 handler。
5. 增加 FIFO dispatcher 队列；消费者失败不影响其他消费者。
6. 每个消费者记录内存 checkpoint、attempt 和 last_error。
7. 补全全部 reducer、`filt`、TTL 和任务状态机。
8. 租约事件化，并实现 TTL、续租、释放、过期和 fencing token。
9. 实现从 offset 0 replay，重建所有视图和租约。
10. Harness 依赖 `BlackboardPort`，不写死具体实现。

建议结构：

```text
blackboard/
  board.py                 # MemoryBlackboard 门面
  dispatcher.py            # FIFO 分发、ack/nack、重试
  validation.py            # 事件和状态转换校验
  leases.py                # 租约规则
  replay.py                # 重放入口
  reducers/
    task.py
    bid.py
    evidence.py
    receipt.py
    authorization.py
    safety.py
    lease.py
```

同步项目接缝修复：

- TASK_POSTED 写完整 TaskPackage。
- BID 查询必须按 task_id。
- 集中式任务也先写 TASK_POSTED。
- 所有执行统一写完整、幂等 RECEIPT。
- claim/release 与对应事件合并成一个原子接口。
- 删除 CLUE 后的手工 `trigger_replan()`。
- AgentLoop 只订阅关心的类型，并检查任务状态和归属。

验收标准：

- 50 个线程并发写事件，无丢失、无重复 offset。
- 一个坏 handler 不影响其他消费者。
- 消费者失败可重试；成功后 checkpoint 前进。
- 晚加入 Agent 不对已完成任务报价。
- replay 后所有 view 和 lease 与运行前一致。
- `skeleton.py` 所有物理动作、授权、租约和重规划都有完整事件链。

### 阶段 2：可选的 SQLite 本地持久化适配器

目标：在不部署外部服务的情况下验证持久化、事务、迁移和重启恢复。

这一阶段是可选的。如果团队已经有 PostgreSQL 环境，可以直接进入阶段 3。

技术：

- Python 标准库 `sqlite3`。
- SQLite WAL 模式。
- SQL migration 文件。
- 同一 `BlackboardPort` 下新增 `SQLiteBlackboard`。

SQLite WAL 允许读写并行，但同一时刻仍然只有一个 writer，因此适合作为本地开发和单进程持久化版本，不作为最终多写者部署方案。[SQLite WAL 官方说明](https://sqlite.org/wal.html)

实现方式：

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

每次 append 使用一个短事务：

```text
BEGIN IMMEDIATE
→ 查询 idempotency_key
→ 检查 aggregate_version
→ INSERT event
→ 更新关键投影/租约
→ COMMIT
```

Python `sqlite3.Connection` 可以通过上下文管理器在正常结束时 commit、异常时 rollback；事务模式必须显式配置，不依赖未来可能变化的默认值。[Python sqlite3 官方文档](https://docs.python.org/3/library/sqlite3.html)

验收标准：

- 杀进程后重启，事件、投影、租约和消费进度不丢失。
- 同一数据库文件重复运行 skeleton 不产生重复任务事件。
- 删除所有投影后可以从 events 重建。
- 对 `SQLITE_BUSY` 有有限重试和清晰错误，不无限阻塞。

### 阶段 3：PostgreSQL 持久化生产核心

目标：支持多个进程/Agent 服务并发读写，以 PostgreSQL 作为黑板唯一事实源。

推荐技术：

- PostgreSQL 当前受支持版本。
- Python 使用 Psycopg 3；若团队已经统一 ORM，可用 SQLAlchemy 2.x Core。
- Alembic 或版本化 SQL 文件管理 migration。
- `JSONB` 保存不同事件的 payload；常用查询字段仍单独建列和索引。PostgreSQL 的 `jsonb` 支持索引，适合事件扩展字段查询。[PostgreSQL JSON 类型文档](https://www.postgresql.org/docs/current/datatype-json.html)

核心能力：

1. 事件持久化和全局 offset。
2. 唯一索引保证幂等。
3. `expected_version` 乐观并发控制。
4. 投影和消费 checkpoint。
5. 原子资源租约。
6. 重放、快照和恢复。
7. 死信记录。

验收标准：

- 多进程并发提交同一幂等键，数据库中只有一条事件。
- 旧 expected_version 稳定返回 Conflict。
- 重放 10 万条事件得到一致投影。
- 数据库备份恢复后 event count、校验和、view 和 lease 一致。
- 服务进程异常退出后从 last_acked_offset 继续。

### 阶段 4：多进程可靠分发（达到条件后再做）

触发条件：

- AgentLoop、Trace、投影器已经拆成不同进程或机器。
- PostgreSQL 轮询延迟/负载不能满足要求。
- 需要断线重连、消费者独立 ACK、积压恢复和水平扩展。

推荐技术：NATS JetStream。

原因：JetStream 提供持久 stream、durable consumer、显式 ACK、重投递和从序号重放；官方说明其消费者可以提供 at-least-once 交付，并跟踪已投递和已确认消息。[NATS JetStream Consumers](https://docs.nats.io/nats-concepts/jetstream/consumers)

PostgreSQL 仍然是唯一事实源，NATS 只负责分发。为避免数据库写成功但消息发布失败，使用 transactional outbox：

```text
同一个 PostgreSQL 事务：
  INSERT bb_events
  INSERT bb_outbox
COMMIT

OutboxPublisher：
  读取未发布 outbox
  → 发布 NATS
  → 收到 server ack
  → 标记 published_at
```

主题建议：

```text
blackboard.task.task_posted
blackboard.task.bid
blackboard.task.task_assigned
blackboard.evidence.clue
blackboard.receipt.receipt
blackboard.human.auth_point
blackboard.safety.estop
```

消费者规则：

- 使用 durable pull consumer。
- 处理成功后 ACK。
- 临时错误 NAK/重试并使用退避。
- 超过次数写 `bb_dead_letters`。
- 消费副作用和 `bb_processed_events` 在同一数据库事务提交。
- 收到重复消息时按 `(consumer_name, event_id)` 跳过副作用后 ACK。
- 不用消息到达顺序代替 `aggregate_version`；发现版本缺口时回查 PostgreSQL。

验收标准：

- 随机 kill publisher/consumer、断网和重复投递后不丢业务事件。
- 每条消息至少处理一次，业务副作用通过消费端幂等只生效一次。
- NATS 停机时 PostgreSQL append 仍可成功，恢复后 outbox 自动补发。
- 10 万条积压恢复过程中无人工改 offset。

### 阶段 5：可靠性、演进和运维硬化

目标：长期运行、排错和升级时仍可证明黑板正确。

开发内容：

- 指标：append 延迟、consumer lag、redelivery、DLQ、projection lag、lease 冲突/过期、replay 耗时。
- 结构化日志：固定包含 event_id、aggregate_id/task_id、correlation_id、offset。
- schema upcaster：旧版本事件读取时升级成新 payload 形状。
- 事件表按时间或任务分区；证据图片/视频只保存 URI 和校验和。
- 快照 + 增量 replay；快照不是事实源，可删除重建。
- 故障演练：重复、乱序、毒消息、时钟漂移、数据库故障、租约持有者失联。

## 10. 技术选型结论

| 能力 | 阶段 | 推荐技术 | 为什么 |
|---|---:|---|---|
| 事件/状态契约 | 0 | Python dataclass + 显式 validator | 保持核心轻量，适合当前标准库架构 |
| 单进程原子性 | 1 | `threading.RLock` + FIFO Queue | 先把并发和分发语义做对 |
| 单元/性质测试 | 0-1 | pytest；可选 Hypothesis | 验证幂等、状态机、并发和 replay 不变量 |
| 本地持久化 | 2 可选 | SQLite + WAL | 零额外服务，验证持久化设计 |
| 最终事实源 | 3 | PostgreSQL + JSONB | 事务、唯一约束、行锁、并发和可靠存储 |
| Python 数据库驱动 | 3 | Psycopg 3 | 直接使用 PostgreSQL 事务与锁语义 |
| 数据库迁移 | 2-3 | 版本化 SQL 或 Alembic | 可重复部署和 schema 演进 |
| 进程内通知 | 1-3 | FIFO Queue；PG 阶段可加 LISTEN/NOTIFY 唤醒 | 事件数据始终从事实表读取 |
| 多进程可靠分发 | 4 可选 | NATS JetStream | durable consumer、ACK、重投递、积压恢复 |
| 大证据对象 | 5 | 对象存储，黑板只存 URI/哈希 | 避免事件表被图片视频撑大 |

### 10.1 为什么当前不先使用 Redis/Kafka

- Redis Pub/Sub 不保存离线期间的消息，不适合作为事件事实源。
- Redis Streams 可以实现可靠消费，但引入它以后仍需要处理事件持久化、视图和租约事务；当前阶段 PostgreSQL 一套事务更简单。
- Kafka 适合高吞吐长期事件流，但运维和分区语义对当前 L0 项目过重。
- NATS JetStream 也不是阶段 0/1 的必需品；只有拆成多进程时才引入。

### 10.2 PostgreSQL LISTEN/NOTIFY 的正确用法

可在阶段 3 用 LISTEN/NOTIFY 减少消费者轮询延迟，但它只作为“有新数据，请查表”的唤醒信号，不能作为事实源。

PostgreSQL 官方文档说明，事务中的 NOTIFY 只有在事务提交后才会发送，而且可以配合数据库表传递结构化数据。[PostgreSQL NOTIFY 文档](https://www.postgresql.org/docs/current/sql-notify.html)

正确流程：

```text
事务写 bb_events
→ COMMIT 后 NOTIFY blackboard_events, '<offset>'
→ consumer 被唤醒
→ 按 last_acked_offset 从 bb_events 查询
```

即使 NOTIFY 丢失，消费者仍能通过周期性查询 offset 补齐事件。

## 11. PostgreSQL 数据模型

### 11.1 事件表 `bb_events`

```sql
CREATE TABLE bb_events (
    offset              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id            TEXT NOT NULL UNIQUE,
    idempotency_key     TEXT,
    ledger              TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    aggregate_id        TEXT NOT NULL,
    task_id             TEXT,
    aggregate_version   BIGINT NOT NULL,
    schema_version      INTEGER NOT NULL DEFAULT 1,
    source              TEXT NOT NULL,
    occurred_at         TIMESTAMPTZ NOT NULL,
    expires_at          TIMESTAMPTZ,
    confidence          DOUBLE PRECISION,
    correlation_id      TEXT,
    causation_id        TEXT,
    payload             JSONB NOT NULL,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (aggregate_id, aggregate_version),
    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

CREATE UNIQUE INDEX uq_bb_events_idempotency
    ON bb_events (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX ix_bb_events_aggregate_offset
    ON bb_events (aggregate_id, offset);

CREATE INDEX ix_bb_events_task_type_offset
    ON bb_events (task_id, event_type, offset);

CREATE INDEX ix_bb_events_ledger_type_offset
    ON bb_events (ledger, event_type, offset);

CREATE INDEX ix_bb_events_payload_gin
    ON bb_events USING GIN (payload);
```

PostgreSQL 的唯一索引与 `INSERT ... ON CONFLICT` 可以原子处理并发幂等写入，`RETURNING` 可返回数据库生成的 offset。[PostgreSQL INSERT 文档](https://www.postgresql.org/docs/current/sql-insert.html)

### 11.2 聚合版本头

不能依赖 `SELECT max(aggregate_version)` 在高并发下生成下一个版本；新聚合还没有事件行时也没有可锁对象。因此为每个聚合维护一行版本头：

```sql
CREATE TABLE bb_aggregate_heads (
    aggregate_id        TEXT PRIMARY KEY,
    current_version     BIGINT NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

append 事务先确保版本头存在，再使用 `SELECT ... FOR UPDATE` 锁住它；校验 `expected_version` 后把 `current_version + 1` 作为新事件版本。同一任务下并发到达的 BID 可以被稳定排序，而 TASK_ASSIGNED 等状态变更可以通过 expected_version 防止重复裁决。

### 11.3 消费进度和投影进度

```sql
CREATE TABLE bb_consumer_offsets (
    consumer_name       TEXT PRIMARY KEY,
    last_acked_offset   BIGINT NOT NULL DEFAULT -1,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE bb_projection_offsets (
    projection_name     TEXT PRIMARY KEY,
    last_offset         BIGINT NOT NULL DEFAULT -1,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 11.4 任务投影示例

```sql
CREATE TABLE bb_task_view (
    task_id             TEXT PRIMARY KEY,
    status              TEXT NOT NULL,
    owner_device_id     TEXT,
    mode                TEXT,
    aggregate_version   BIGINT NOT NULL,
    data                JSONB NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL
);
```

证据、回执、授权和安全视图可以分别建表，避免一个通用 view 表失去约束。所有投影表都可通过 replay 重建。

### 11.5 租约表

```sql
CREATE TABLE bb_leases (
    resource_id         TEXT PRIMARY KEY,
    holder_id           TEXT NOT NULL,
    task_id             TEXT,
    fencing_token       BIGINT NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,
    version             BIGINT NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 11.6 失败和可选 outbox 表

```sql
CREATE TABLE bb_dead_letters (
    consumer_name       TEXT NOT NULL,
    event_offset        BIGINT NOT NULL,
    attempts            INTEGER NOT NULL,
    error               TEXT NOT NULL,
    payload             JSONB NOT NULL,
    first_failed_at     TIMESTAMPTZ NOT NULL,
    last_failed_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (consumer_name, event_offset)
);

CREATE TABLE bb_outbox (
    event_offset        BIGINT PRIMARY KEY REFERENCES bb_events(offset),
    subject             TEXT NOT NULL,
    payload             JSONB NOT NULL,
    published_at        TIMESTAMPTZ,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT
);

CREATE TABLE bb_processed_events (
    consumer_name       TEXT NOT NULL,
    event_id            TEXT NOT NULL,
    processed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_name, event_id)
);
```

`bb_outbox` 和 `bb_processed_events` 到阶段 4 再启用。

## 12. 关键算法怎么实现

### 12.1 原子 append

```text
BEGIN
  1. 按 idempotency_key 查询已有事件；存在则返回原 AppendResult。
  2. 确保 bb_aggregate_heads 中存在该 aggregate，并锁定版本头。
  3. 如果 expected_version 不匹配，返回 VersionConflict。
  4. 校验事件状态转换和 payload。
  5. INSERT bb_events，RETURNING offset。
  6. 同事务更新要求强一致的当前投影。
  7. 阶段 4 同事务 INSERT bb_outbox。
COMMIT
  8. 唤醒 dispatcher；分发失败不能回滚已经提交的事件。
```

不要在事务提交前调用 Agent handler。

### 12.2 幂等 append

伪代码：

```python
def append(event, expected_version=None):
    existing = find_by_idempotency_key(event.idempotency_key)
    if existing:
        return AppendResult.from_existing(existing, duplicate=True)

    try:
        return insert_in_transaction(event, expected_version)
    except UniqueViolation:
        existing = find_by_idempotency_key(event.idempotency_key)
        return AppendResult.from_existing(existing, duplicate=True)
```

应用层先查只是优化，数据库唯一约束才是并发下的最终保证。

### 12.3 消费、ACK 和重试

```text
读取 last_acked_offset
→ 查询 offset > last_acked_offset 的下一批事件
→ 按订阅类型过滤
→ 调用 handler
→ 成功：保存 processed_event + 前移 checkpoint
→ 临时失败：记录 attempt，退避后重试
→ 永久失败：写 DLQ，再按策略决定是否越过
```

不同消费者拥有不同 checkpoint。Trace 失败不能阻塞 AgentLoop；一个 AgentLoop 失败也不能阻塞其他 Agent。

### 12.4 投影与 replay

Reducer 必须是确定性的纯函数：

```python
new_state = reduce(old_state, event)
```

重放：

```text
清空指定投影
→ 从 offset 0（或可信快照 offset）读取事件
→ 按 offset 顺序执行 reducer
→ 每批提交 projection_offset
→ 对比事件数、最终版本和状态哈希
```

投影不能产生新的领域事件，否则 replay 会造成重复副作用。需要产生事件的业务判断应由独立消费者负责，并使用幂等键。

### 12.5 原子 claim

PostgreSQL 版本建议使用短事务和行锁：

```text
BEGIN
  SELECT * FROM bb_leases
   WHERE resource_id = :resource_id
   FOR UPDATE;

  如果无记录或已过期：
    新 holder 获得租约，fencing_token + 1
    INSERT/UPDATE bb_leases
    INSERT CLAIM_GRANTED event

  如果同一 holder：
    续租，不降低 token
    INSERT LEASE_RENEWED event

  如果其他 holder 仍有效：
    不修改租约
    INSERT CLAIM_REJECTED event
COMMIT
```

`SELECT ... FOR UPDATE` 会阻止其他事务同时修改或锁定同一行，直至当前事务结束。[PostgreSQL 行锁文档](https://www.postgresql.org/docs/current/explicit-locking.html)

### 12.6 TTL

- 历史事件不删除。
- 查询当前 evidence 时忽略 `expires_at <= now()` 的线索。
- 过期租约由定时 worker 写 `LEASE_EXPIRED`，随后清除/更新 lease_view。
- 不依赖设备本地时钟决定最终租约，以数据库 UTC 时间为准。

## 13. 对当前代码的修改映射

| 当前文件 | 阶段 0/1 修改 |
|---|---|
| `contracts/blackboard_event.py` | 扩展事件信封、冻结对象、补事件类型 |
| `contracts/interfaces.py` | 升级 append/query/subscribe/ack/lease 接口 |
| `blackboard/board.py` | 原子内存 append、幂等 offset、dispatcher、checkpoint |
| `blackboard/view.py` | 拆 reducer，补全部事件、filter、TTL、状态机 |
| `runtime/harness.py` | 依赖 BlackboardPort；保存订阅 ID；按类型订阅 |
| `runtime/skeleton.py` | 完整 TASK_POSTED；按 task_id 读 BID；移除重复 replan |
| `coordination/agent_loop.py` | 读取完整任务上下文；完整 RECEIPT；处理 assignment/replan |
| `coordination/context_assembler.py` | 按 task_id 查询 task/evidence/receipt/lease view |
| `coordination/conflict.py` | 使用原子 Lease API，不再直接改字典 |
| `coordination/master.py` | 集中任务也必须先 TASK_POSTED；判给幂等 |
| `safety/reversibility.py` | 授权状态由事件恢复；批准后才执行；完整回执 |
| `safety/estop.py` | 保留旁路控制，完善 ESTOP 审计 payload 与幂等策略 |
| `ingress/event_ingress.py` | 标准化设备/环境事件后调用 append |

阶段 2/3 再新增：

```text
blackboard/storage/base.py
blackboard/storage/sqlite.py       # 可选
blackboard/storage/postgres.py
blackboard/migrations/
blackboard/outbox.py               # 阶段 4
```

## 14. 测试计划

### 14.1 契约测试

- 每个事件合法 payload 能写入。
- 缺字段和错误字段类型被拒绝。
- type 与 ledger 不匹配被拒绝。
- confidence、时间和版本边界正确。

### 14.2 幂等与版本测试

- 相同 key 重复 100 次只落一条，返回相同 offset。
- 不同 key 可以产生不同事件。
- 旧 expected_version 被拒绝。
- 两个并发 TASK_ASSIGNED 只能成功一个。

### 14.3 订阅测试

- 类型过滤正确。
- Trace、多个 Agent 拥有独立 checkpoint。
- handler 抛异常不影响其他消费者。
- 重试后不会重复业务副作用。
- late join Agent 不处理已结束旧任务。

### 14.4 reducer/replay 测试

- posted → assigned → done 得到正确任务状态。
- 非法 done-before-posted 被拒。
- 删除投影后重放结果一致。
- 过期 CLUE 保留历史但不进入活动 evidence_view。
- ESTOP activate/clear 得到正确当前状态。

### 14.5 租约测试

- 同一资源同时只有一个持有者。
- 同一持有者续租成功。
- 非持有者不能释放。
- 过期后新持有者 token 增加。
- 旧 token 无法执行受保护动作。
- replay 后租约状态一致。

### 14.6 skeleton 集成测试

至少断言：

```text
首任务有完整 TASK_POSTED
dog_a/dog_b 各一条 BID
判给只使用当前 task 的 BID
R0 和 R1 动作都有完整 RECEIPT
claim 成功、失败、release/仲裁均有事件
集中式任务也有 TASK_POSTED
每个 CLUE 只触发一次重规划
急停失败回执包含 failure_reason
TASK_DONE 包含结果和证据
最终投影可由事件重建
```

## 15. 每阶段交付物

| 阶段 | 交付物 | 完成判据 |
|---:|---|---|
| 0 | 事件信封、typed payload、状态机、幂等规则、契约测试 | 非法事件不能进入黑板 |
| 1 | 完整 MemoryBlackboard、dispatcher、reducers、Lease、replay、集成测试 | skeleton 黑板链路完整且可重放 |
| 2 | 可选 SQLite adapter、migration、重启恢复测试 | 单机重启不丢状态 |
| 3 | PostgreSQL adapter、事务/CAS、持久 checkpoint、DLQ | 多进程并发和恢复正确 |
| 4 | 可选 NATS JetStream、outbox、幂等消费者 | 断网/重复/积压下不丢业务事实 |
| 5 | 指标、归档、schema 演进、故障演练 | 长期运行与升级可验证 |

## 16. 建议从哪里开始编码

第一批只做阶段 0，不同时引入数据库：

1. 列出 skeleton 所需 payload dataclass。
2. 扩展 `BlackboardEvent`，定义 `AppendResult/EventQuery`。
3. 给每个事件制定幂等键。
4. 写事件校验和状态机测试。
5. 修改 skeleton 的事件 payload，确保数据不丢。

第二批做阶段 1：

1. 改造 MemoryBlackboard 原子写入。
2. 将同步 handler 改为队列 dispatcher。
3. 实现完整 reducers 和 replay。
4. 实现事件化 Lease。
5. 修复 BID 隔离、R1 回执、集中任务发布和重复 replan。

阶段 1 验收后，`BlackboardPort` 的语义应基本稳定；此时再写 SQLite/PostgreSQL adapter，存储替换才不会反复返工。

## 17. 最终定义：什么叫“黑板功能做完”

在不考虑前端的前提下，至少满足以下条件才算核心黑板完成：

- skeleton 的每个协同事实都有完整、合法、可关联的事件。
- 同一逻辑事件可安全重试，不会重复产生副作用。
- 多任务的报价、线索、回执和状态不会串数据。
- 一个消费者失败不会影响事件提交和其他消费者。
- 任务、证据、回执、授权、安全和租约都有完整当前视图。
- 租约具备原子性、TTL、续租和 fencing token。
- 系统重启后能恢复事件、视图、租约、pending 授权和消费位置。
- 所有投影都能从事件流重建。
- PostgreSQL 是唯一事实源；消息中间件只是可重放分发层。
- 并发、重复、乱序、失败和 replay 都有自动化测试证明。

达到阶段 3，黑板核心可以认为“功能完整且可用于多进程开发”；阶段 4/5 是根据部署规模增加的可靠分发和生产硬化能力。

## 18. 官方技术参考

- [Python `sqlite3` 官方文档](https://docs.python.org/3/library/sqlite3.html)
- [SQLite WAL 官方文档](https://sqlite.org/wal.html)
- [PostgreSQL `INSERT ... ON CONFLICT` 官方文档](https://www.postgresql.org/docs/current/sql-insert.html)
- [PostgreSQL JSON/JSONB 官方文档](https://www.postgresql.org/docs/current/datatype-json.html)
- [PostgreSQL 行锁官方文档](https://www.postgresql.org/docs/current/explicit-locking.html)
- [PostgreSQL LISTEN/NOTIFY 官方文档](https://www.postgresql.org/docs/current/sql-notify.html)
- [NATS JetStream Consumers 官方文档](https://docs.nats.io/nats-concepts/jetstream/consumers)
