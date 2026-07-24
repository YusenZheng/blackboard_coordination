# SwarmBrain 黑板层间协作与数据契约

> 依据：系统架构图与完整 `runtime/skeleton.py`
> 版本：MVP 1.0 契约冻结草案 + 2.0 优化方案
> 范围：只考虑代码实现与终端展示；暂不考虑前端、HTTP API、SSE/WebSocket 和真实设备通信协议

## 1. 要解决的问题

项目中的入口层、黑板层、协同层、安全层、接入层和资产层分别由不同模块负责。如果各模块自行定义数据字典，最后联调时容易出现：

- 同一个字段使用不同名字。
- 入口层提供的信息不足以让协同层决策。
- 黑板保存了事件，但无法按 task_id 隔离。
- 执行失败、授权、租约或重规划没有形成完整记录。
- 模块间依赖当前同步实现，替换实现后流程失效。

本文冻结 MVP 1.0 的最小数据契约，确保所有层围绕同一个黑板正确流转；同时给出 2.0 的可靠性和工程化升级方向。

## 2. 总体

### 2.1 MVP 1.0 的目标

MVP 不是把所有未来能力做完，而是让完整 `skeleton.py` 中涉及黑板的协同事实都具备：

```text
统一事件格式
→ 能写入
→ 能按任务读取
→ 能通知正确消费者
→ 能折叠为当前状态
→ 能在终端看见
```

MVP 保留：

- 单 Python 进程。
- 内存事件列表。
- 同步订阅回调。
- mock 设备。
- 写死的报价规则。
- 简单内存租约。
- 标准库终端监视器。

MVP 暂不做：

- PostgreSQL、Redis、NATS、Kafka。
- 多进程并发。
- 真正 ACK/重试。
- 真 LLM。
- 前端和外部 API。
- 真机、弱网和固件协议。

### 2.2 2.0 的目标

2.0 在不改变业务事件含义的前提下，把黑板升级为：

- 持久化事件存储。
- 异步可靠分发。
- 消费 offset/ACK/重试。
- 多任务和多进程并发安全。
- 可 replay 的完整派生视图。
- 具备 TTL、续租和 fencing token 的资源租约。
- 可恢复 pending 授权、任务和消费进度。
- 更清晰的终端 TUI 观测。

## 3. 黑板在架构图中的位置

按照架构图，黑板位于群智内核中，连接关系如下：

```text
                              ┌──────────────────┐
                              │ contracts 契约层 │
                              └────────┬─────────┘
                                       │ 定义共同数据格式
                                       ▼
┌──────────────┐  TASK_POSTED   ┌───────────────┐  TASK/BID/CLUE   ┌──────────────┐
│ ingress      │ ─────────────→ │ blackboard    │ ←──────────────→ │ coordination │
│ 北向入口层    │                │ 群智空间层     │                  │ 协同层        │
└──────────────┘                └───────┬───────┘                  └──────┬───────┘
                                       │                                 │ ActionIntent
                       全事件订阅       │                                 ▼
                                       │                         ┌──────────────┐
                              ┌────────▼──────┐                  │ safety       │
                              │ assets/Trace  │                  │ 安全平面      │
                              │ 资产层         │                  └──────┬───────┘
                              └───────────────┘                         │ 已放行动作
                                                                       ▼
                                                               ┌──────────────┐
                                                               │ access       │
                                                               │ 南向接入层    │
                                                               └──────┬───────┘
                                                                      │ ActionReceipt
                                                                      └→ AgentLoop → 黑板 RECEIPT
```

安全层还会向黑板写 AUTH_POINT、AUTH_DECISION 和 ESTOP 审计事件。接入层的高频遥测不经过黑板，直接更新 Registry 并进入 Trace。

## 4. 黑板与每个层的协作边界

| 层 | 向黑板输入 | 从黑板接收 | 黑板不负责的部分 |
|---|---|---|---|
| contracts | 不产生运行数据；定义 Event/Task/Intent/Receipt 格式 | 不消费事件 | 不实现业务逻辑 |
| ingress | TASK_POSTED；2.0 增加设备/环境标准事件接入 | 可查询任务是否已存在 | 不负责报价和判给 |
| coordination | BID、TASK_ASSIGNED、RECEIPT、TASK_REPLAN；协同动词事件 | TASK_POSTED、CLUE、TASK_ASSIGNED、当前任务/线索/租约视图 | 不直接改黑板内部状态 |
| safety | AUTH_POINT、AUTH_DECISION、ESTOP 留痕；未来 SAFETY_INTERCEPT | 可读取任务约束、授权状态、急停状态 | 黑板不执行安全判定 |
| access | 通过 AgentLoop 间接写 RECEIPT/CLUE；2.0 EventIngress 标准化上报 | 业务动作由 coordination/safety 传入，不直接靠黑板下发 | 高频遥测不灌入黑板 |
| assets | Trace 订阅全部协同事件；运营账本从 Trace 聚合 | 完整事件流 | Trace 不重复生产同一协同事实 |
| memory | MVP 不接入；2.0 可读取任务/证据视图形成共享事实 | task/evidence 视图 | 私有记忆不直接写协同状态 |
| runtime | 创建黑板、装配生产者和消费者 | 持有 BlackboardPort | 不定义事件字段 |
| sim/eval | MVP 不接入；2.0 可生产仿真事件、读取 Trace/事件 | 事件历史和投影 | 不修改线上事实语义 |
| api/前端 | 当前不做 | 当前不做 | 用终端监视器替代 |

## 5. MVP 1.0 统一事件信封

MVP 保持当前 `BlackboardEvent` 的整体结构，避免第一版大规模重构。

```python
@dataclass
class BlackboardEvent:
    id: str
    type: EventType
    ledger: Ledger
    content: dict
    source: str
    ts: float = 0.0
    confidence: float | None = None
    ttl: float | None = None
    version: int = 0
    idempotency_key: str | None = None
    extra: dict = field(default_factory=dict)
```

### 5.1 字段责任

| 字段 | MVP 是否必填 | 谁填写 | 规则 |
|---|---:|---|---|
| `id` | 是 | 事件生产者 | 同一事件稳定不变；格式见后文 |
| `type` | 是 | 事件生产者 | 必须使用 EventType |
| `ledger` | 是 | 事件生产者 | 必须与事件类型匹配 |
| `content` | 是 | 事件生产者 | 必须符合对应 payload 契约 |
| `source` | 是 | 事件生产者 | ingress/coord/safety/human/device_id 等 |
| `ts` | 否 | 黑板 | 为 0 时由 append 自动填 UTC epoch 秒 |
| `confidence` | CLUE 必填 | 线索生产者 | 0～1 |
| `ttl` | 否 | 生产者 | MVP 记录但暂不自动过期 |
| `version` | 否 | 黑板 | 为 0 时由黑板递增分配 |
| `idempotency_key` | 领域事件必填 | 生产者 | 相同逻辑事件重试只能生效一次 |
| `extra` | 否 | 生产者 | 只放非核心扩展字段，不得藏必填字段 |

### 5.2 全局字段规则

1. 所有与任务相关的事件，`content.task_id` 必填。
2. 所有与设备相关的事件，`content.device_id` 必填。
3. 所有与动作相关的事件，`content.intent_id` 必填。
4. 所有与资源相关的事件，统一使用 `content.resource_id`，不再混用 `resource`。
5. 所有状态字段使用小写英文枚举字符串。
6. 所有 idempotency_key 使用冒号分段的稳定格式。

### 5.3 MVP 内部 Python 接口

这里的接口是模块间 Python 契约，不是 HTTP API。

```python
class BlackboardPort(Protocol):
    def append(self, event: BlackboardEvent) -> int: ...

    def read_events(
        self,
        since_offset: int = 0,
        types: list[EventType] | None = None,
        task_id: str | None = None,
    ) -> list[BlackboardEvent]: ...

    def query_view(
        self,
        ledger: Ledger | None = None,
        filt: dict | None = None,
    ) -> dict: ...

    def subscribe(
        self,
        handler,
        types: list[EventType] | None = None,
        from_offset: int = 0,
    ) -> str: ...

    def claim(self, resource_id: str, device_id: str) -> bool: ...
    def release(self, resource_id: str, device_id: str) -> bool: ...
```

MVP 必须补 `task_id` 过滤。协调层不能再读取所有任务的 BID。

## 6. 入口层 ingress ↔ 黑板

### 6.1 入口层输入

```python
raw_input: str
media: optional
```

示例：

```text
帮我找走失的萨摩耶幼犬
```

### 6.2 入口层内部输出 TaskPackage

```python
TaskPackage(
    task_id="find_dog_001",
    task_type="search_target",
    goal="帮我找走失的萨摩耶幼犬",
    success_condition="目标被近距离确认",
    safety_constraints=[],
    requirement=TaskRequirement(
        required_capabilities=["G01"],
        min_battery=0.2,
    ),
    target_profile={...},
    area=Position(label="公园", area="公园"),
    priority="high",
    initial_autonomy_level="A1",
    extra={"mode": "autonomous"},
)
```

### 6.3 入口层写入黑板 TASK_POSTED

MVP 冻结格式：

```python
BlackboardEvent(
    id="post-find_dog_001",
    type=EventType.TASK_POSTED,
    ledger=Ledger.TASK,
    source="ingress",
    idempotency_key="task:find_dog_001:posted",
    content={
        "task_id": "find_dog_001",
        "task_type": "search_target",
        "goal": "帮我找走失的萨摩耶幼犬",
        "success_condition": "目标被近距离确认",
        "safety_constraints": [],
        "requirement": {
            "required_capabilities": ["G01"],
            "min_battery": 0.2,
        },
        "target_profile": {
            "species": "萨摩耶幼犬",
            "color": "白",
            "collar": "红",
        },
        "area": {"label": "公园", "area": "公园"},
        "priority": "high",
        "initial_autonomy_level": "A1",
        "mode": "autonomous",
    },
)
```

黑板输入：TASK_POSTED 事件。

黑板直接输出：`offset: int`。

黑板间接输出：

- 通知订阅 TASK_POSTED 的 AgentLoop。
- task_view 新增任务，状态为 `posted`。
- Trace 收到任务事件。

当前 skeleton 只写 `task_id + goal`，MVP 必须改为完整任务字段，否则协调层无法独立工作。

## 7. 协同层 coordination ↔ 黑板

### 7.1 黑板向 AgentLoop 输出 TASK_POSTED

回调格式：

```python
def on_event(event: BlackboardEvent, offset: int) -> None:
    ...
```

协调层读取：

```text
event.type
event.content.task_id
event.content.task_type
event.content.goal
event.content.requirement
event.content.priority
event.content.mode
```

### 7.2 AgentLoop 向黑板写 BID

```python
BlackboardEvent(
    id="bid-find_dog_001-dog_a",
    type=EventType.BID,
    ledger=Ledger.TASK,
    source="dog_a",
    idempotency_key="task:find_dog_001:bid:dog_a",
    content={
        "task_id": "find_dog_001",
        "device_id": "dog_a",
        "decision": "accept",
        "bid_score": 1.0,
        "reason_codes": ["battery_ok", "capability_match"],
    },
)
```

BID 字段：

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| task_id | str | 是 | 必须对应已发布任务 |
| device_id | str | 是 | 报价设备 |
| decision | str | 是 | accept/reject |
| bid_score | float | 是 | MVP 统一 0～1 |
| reason_codes | list[str] | 是 | 使用稳定原因代码，不用拼接中文字符串做逻辑 |

### 7.3 协调器从黑板读取当前任务报价

```python
bids = blackboard.read_events(
    types=[EventType.BID],
    task_id="find_dog_001",
)
```

黑板输出：

```python
list[BlackboardEvent]  # 只包含 find_dog_001 的 BID
```

MVP 允许同步订阅后立即收集报价，但集成测试必须固定注册两台设备并断言得到两条 BID。2.0 改为竞价窗口和异步收集。

### 7.4 协调层向黑板写 TASK_ASSIGNED

```python
BlackboardEvent(
    id="assign-find_dog_001",
    type=EventType.TASK_ASSIGNED,
    ledger=Ledger.TASK,
    source="coord",
    idempotency_key="task:find_dog_001:assigned",
    content={
        "task_id": "find_dog_001",
        "device_id": "dog_a",
        "assignment_mode": "autonomous_bid",
        "winning_bid_score": 1.0,
    },
)
```

黑板输出：

- task_view 状态变成 `assigned`。
- task_view.owner 变成 `dog_a`。
- AgentLoop/Trace 收到判给事件。

### 7.5 协同层执行后向黑板写 RECEIPT

ActionIntent 本身走协调层 → 安全层 → 接入层的直接调用，不必在 MVP 进入黑板。设备执行结果必须进入黑板：

```python
BlackboardEvent(
    id="rcpt-mv-1",
    type=EventType.RECEIPT,
    ledger=Ledger.RECEIPT,
    source="dog_a",
    idempotency_key="intent:mv-1:receipt",
    content={
        "task_id": "find_dog_001",
        "intent_id": "mv-1",
        "device_id": "dog_a",
        "verb": "move_to",
        "success": True,
        "result": {"position": "waypoint_A"},
        "failure_reason": None,
        "recovery_class": None,
        "partial_progress": None,
        "duration_s": 0.0,
    },
)
```

当前代码只写 `intent_id/device_id/success`，MVP 必须补齐完整 ActionReceipt 字段。

## 8. 安全层 safety ↔ 黑板

### 8.1 R0 动作

R0 安全判定不强制单独写事件。结果可以随 RECEIPT 保存：

```json
{
  "safety_level": "R0",
  "authorized": true
}
```

如果动作被 R2 拦截，MVP 可以写 RECEIPT `success=false`；2.0 再增加 SAFETY_INTERCEPT 专用投影。

### 8.2 R1 授权请求 AUTH_POINT

```python
BlackboardEvent(
    id="auth-enter-1",
    type=EventType.AUTH_POINT,
    ledger=Ledger.HUMAN,
    source="safety",
    idempotency_key="intent:enter-1:auth-request",
    content={
        "task_id": "find_dog_001",
        "intent_id": "enter-1",
        "device_id": "dog_a",
        "risk_level": "R1",
        "action": "move_to",
        "params": {"target": "绿篱后缝隙", "enclosed": True},
        "reason": "irreversible_action",
        "status": "pending",
    },
)
```

### 8.3 授权结果 AUTH_DECISION

```python
BlackboardEvent(
    id="authd-enter-1",
    type=EventType.AUTH_DECISION,
    ledger=Ledger.HUMAN,
    source="human",
    idempotency_key="intent:enter-1:auth-decision",
    content={
        "task_id": "find_dog_001",
        "intent_id": "enter-1",
        "approved": True,
        "approver": "指挥官",
        "reason": "confirmed",
    },
)
```

黑板输出：authorization_view 从 `pending` 变成 `approved/rejected`。

MVP 规则：只有 `approved=True` 才允许继续执行；授权后执行必须经 AgentLoop.act，并写完整 RECEIPT。当前 skeleton 直接 dispatch 且不写回执，需要修复。

### 8.4 急停 ESTOP

急停控制旁路黑板先作用于设备；黑板只保存审计事实。

激活：

```python
content={
    "scope": "all",
    "state": "activated",
    "reason": "演示:一键急停",
}
```

解除：

```python
content={
    "scope": "all",
    "state": "cleared",
    "reason": "operator_clear",
}
```

事件 ID 和幂等键必须包含动作状态或操作序号，不能让多次急停使用完全相同的事件身份。

## 9. 接入层 access ↔ 黑板

### 9.1 动作下行

MVP 数据流：

```text
coordination 产生 ActionIntent
→ safety.check(intent)
→ AgentLoop.act(intent)
→ ToolGateway.dispatch(intent)
→ Adapter.execute(intent)
```

这条下行调用不需要黑板逐层转发。黑板只接收执行后的 RECEIPT，防止它变成设备指令队列。

### 9.2 执行回执上行

```text
Adapter → ActionReceipt → AgentLoop → RECEIPT 事件 → 黑板
```

输入格式使用第 7.5 节冻结的完整 RECEIPT。

### 9.3 设备发现线索

MVP 中由 skeleton 手工构造 CLUE；未来由 Tool 回执或 EventIngress 转换。

```python
BlackboardEvent(
    id="clue-1",
    type=EventType.CLUE,
    ledger=Ledger.EVIDENCE,
    source="dog_a",
    confidence=0.6,
    ttl=300.0,
    idempotency_key="task:find_dog_001:clue:clue-1",
    content={
        "task_id": "find_dog_001",
        "clue_id": "clue-1",
        "position": {"label": "waypoint_B", "area": "片区B"},
        "description": "发现疑似目标",
        "confidence_level": "mid",
        "confidence_method": "mock",
        "source_device": "dog_a",
    },
)
```

黑板输出：

- evidence_view 新增 clue-1。
- 只通知订阅 CLUE 的协调消费者。
- Trace 保存完整线索。

### 9.4 高频遥测旁路

```text
TelemetrySample
→ TelemetryChannel
→ Registry.AgentCard.state
→ Trace telemetry bucket
```

这不是黑板数据流。每秒几十条位置、电量和姿态不能全部灌入四账本。

只有具有协同意义的状态变化才转成低频事件，例如 2.0 的：

- DEVICE_LOW_BATTERY
- DEVICE_OFFLINE
- DEVICE_STUCK
- CAPABILITY_CHANGED

## 10. 资产层 assets ↔ 黑板

### 10.1 Trace

Trace 是全事件订阅者：

```python
blackboard.subscribe(trace.on_event)

def on_event(event: BlackboardEvent, offset: int) -> None:
    trace_events.append((offset, event))
```

黑板输入给 Trace：`offset + 完整事件`。

Trace 输出：事件冷档、后续评测和复盘数据。

约束：Trace 不再创建另一条相同协同事件；黑板是事实源，Trace 是派生副本。

### 10.2 Skill

当前 skeleton 中 SkillGraph 由 AgentLoop 直接检索，不经过黑板。MVP 保持不变。

2.0 可在技能注册或版本变化时写 SKILL_REGISTERED，但 Skill 大内容仍放资产存储，黑板只保存：

```text
skill_id/version/signature/source/registered_at
```

### 10.3 运营账本

当前运营 Ledger 从 Trace 统计：

```text
TASK_POSTED 数量
RECEIPT 数量
AUTH_POINT 数量
```

因此所有业务路径必须完整写黑板，否则运营统计必然漏数。集中式任务也必须先写 TASK_POSTED，R1 动作也必须写 RECEIPT。

## 11. 资源冲突：黑板 ↔ ConflictService

MVP 可以保留 `claim/release` 内存字典，但每次结果必须事件化。

### 11.1 claim 输入

```python
resource_id = "passage_X"
device_id = "dog_a"
task_id = "find_dog_001"
```

### 11.2 claim 返回

```python
granted: bool
```

### 11.3 CLAIM 事件

无论成功还是失败都写一条：

```python
BlackboardEvent(
    id="claim-find_dog_001-passage_X-dog_a",
    type=EventType.CLAIM,
    ledger=Ledger.RECEIPT,
    source="dog_a",
    idempotency_key="task:find_dog_001:claim:passage_X:dog_a",
    content={
        "task_id": "find_dog_001",
        "resource_id": "passage_X",
        "device_id": "dog_a",
        "granted": True,
        "current_holder": "dog_a",
        "reason": "resource_available",
    },
)
```

dog_b 失败时：

```json
{
  "task_id": "find_dog_001",
  "resource_id": "passage_X",
  "device_id": "dog_b",
  "granted": false,
  "current_holder": "dog_a",
  "reason": "held_by_other_device"
}
```

### 11.4 YIELD 事件

MVP 中 YIELD 表示设备主动退出资源竞争或释放自己持有的资源：

```python
content={
    "task_id": "find_dog_001",
    "resource_id": "passage_X",
    "device_id": "dog_b",
    "had_lease": False,
    "released": False,
    "reason": "yield_after_claim_rejected",
}
```

如果持有者释放：

```python
content={
    "task_id": "find_dog_001",
    "resource_id": "passage_X",
    "device_id": "dog_a",
    "had_lease": True,
    "released": True,
    "reason": "resource_use_finished",
}
```

当前 skeleton 只打印 yield，没有 release 和 YIELD 事件，MVP 要补齐。

## 12. 四条链路的完整黑板数据流

### 12.1 链路一：正常任务主链

```text
ingress
  │ TASK_POSTED
  ▼
blackboard
  │ TASK_POSTED delivery
  ▼
AgentLoop dog_a / dog_b
  │ BID × 2
  ▼
blackboard
  │ 当前 task_id 的 BID[]
  ▼
ConflictService.rank_bids
  │ TASK_ASSIGNED
  ▼
blackboard
  │ owner=dog_a
  ▼
coordination → safety → access
  │ ActionReceipt
  ▼
AgentLoop
  │ RECEIPT
  ▼
blackboard → Trace / receipt_view
```

| 步骤 | 黑板输入 | 黑板输出 |
|---|---|---|
| 发布任务 | TASK_POSTED | offset；通知 Agent；task_view=posted |
| 设备应征 | BID × N | offset；按 task_id 查询 BID[] |
| 任务判给 | TASK_ASSIGNED | task_view=assigned/owner |
| 动作执行 | RECEIPT | receipt_view；Trace |

### 12.2 链路二：授权和资源冲突

```text
R1 ActionIntent
→ AUTH_POINT
→ AUTH_DECISION
→ approved 后执行
→ RECEIPT

claim dog_a → CLAIM(granted=true)
claim dog_b → CLAIM(granted=false)
dog_b 让位 → YIELD
仲裁结果 → MVP 写 CLAIM/YIELD 的最终状态；2.0 增加专用仲裁事件
```

| 步骤 | 黑板输入 | 黑板输出 |
|---|---|---|
| 请求授权 | AUTH_POINT | authorization_view=pending |
| 人工决定 | AUTH_DECISION | approved/rejected |
| 授权执行 | RECEIPT | 完整执行结果 |
| 资源申请 | CLAIM | lease_view/申请历史 |
| 主动让位 | YIELD | lease_view 更新 |

### 12.3 链路三：集中式 Master

自主模式和集中模式只在“如何产生 TASK_ASSIGNED”上不同。

正确数据流：

```text
ingress → TASK_POSTED(mode=master_worker) → blackboard
Master 从 Registry 选择设备
Master → TASK_ASSIGNED(assignment_mode=master_worker) → blackboard
```

集中式 TASK_ASSIGNED：

```python
content={
    "task_id": "find_dog_002",
    "device_id": "dog_a",
    "assignment_mode": "master_worker",
    "winning_bid_score": None,
}
```

当前 skeleton 第二个任务没有 TASK_POSTED，MVP 必须补上，否则黑板只有一个没有任务内容的 assigned 空壳。

### 12.4 链路四：新线索与重协同

```text
access/设备发现线索
→ CLUE
→ blackboard
→ AgentLoop.on_event(CLUE)
→ coordination 重新评估
→ TASK_REPLAN
→ 后续可产生 BID/YIELD/HANDOFF
```

MVP 的 TASK_REPLAN 只记录“已触发重新评估”，不要求实现复杂算法：

```python
BlackboardEvent(
    id="replan-find_dog_001-clue-1",
    type=EventType.TASK_REPLAN,
    ledger=Ledger.TASK,
    source="coord",
    idempotency_key="task:find_dog_001:replan:clue-1",
    content={
        "task_id": "find_dog_001",
        "clue_id": "clue-1",
        "reason": "new_clue",
        "decision": "reevaluate",
        "affected_device_ids": ["dog_a", "dog_b"],
    },
)
```

重要：`append(CLUE)` 已经通过订阅触发 AgentLoop。当前 skeleton 随后又调用 `trigger_replan()`，会重复触发。MVP 只保留一次事件驱动路径。

## 13. 收尾流程的数据契约

### 13.1 急停失败回执

急停期间动作失败仍然写 RECEIPT：

```python
content={
    "task_id": "find_dog_001",
    "intent_id": "after-estop",
    "device_id": "dog_a",
    "verb": "move_to",
    "success": False,
    "result": {},
    "failure_reason": "device_estopped",
    "recovery_class": "need_human",
    "partial_progress": None,
    "duration_s": 0.0,
}
```

### 13.2 任务完成 TASK_DONE

```python
BlackboardEvent(
    id="done-find_dog_001",
    type=EventType.TASK_DONE,
    ledger=Ledger.TASK,
    source="coord",
    idempotency_key="task:find_dog_001:done",
    content={
        "task_id": "find_dog_001",
        "success": True,
        "completed_by": "dog_a",
        "success_condition_met": True,
        "evidence_ids": ["clue-1"],
        "result_summary": "目标已近距离确认",
    },
)
```

黑板输出：task_view 状态变为 `done`；Trace 和运营统计收到完成事件。

## 14. MVP 派生视图输出格式

`query_view()` 最少返回：

```python
{
    "tasks": {
        "find_dog_001": {
            "status": "assigned",
            "owner": "dog_a",
            "mode": "autonomous",
            "content": {...},
        }
    },
    "bids": {
        "find_dog_001": {
            "dog_a": {...},
            "dog_b": {...},
        }
    },
    "evidence": {
        "clue-1": {...}
    },
    "receipts": {
        "mv-1": {...}
    },
    "human": {
        "enter-1": {
            "status": "approved",
            "request": {...},
            "decision": {...},
        }
    },
    "leases": {
        "passage_X": {
            "holder": "dog_a",
            "status": "active",
        }
    },
    "safety": {
        "all": {
            "estop": False,
            "reason": None,
        }
    },
    "replans": {
        "find_dog_001": {...}
    },
}
```

MVP 可以每次查询时从事件列表全量折叠，不需要物化视图。

## 15. MVP 终端黑板监视器

MVP 不做前端，用一个只读终端订阅者展示全部黑板信息。

### 15.1 数据流

```text
Blackboard.append(event)
  ├→ Trace.on_event(event, offset)
  ├→ AgentLoop.on_event(event, offset)
  └→ ConsoleBlackboardMonitor.on_event(event, offset)
          ├→ 打印一行事件
          ├→ 关键事件后打印当前视图摘要
          └→ 可选写入 JSONL 文件
```

### 15.2 MVP 文件结构

```text
runtime/
  monitor/
    __init__.py
    console_monitor.py
    event_formatter.py
    jsonl_recorder.py       # 可选
```

### 15.3 一行事件格式

```text
[BB][000000][TASK][TASK_POSTED] source=ingress task=find_dog_001 goal=帮我找走失的萨摩耶幼犬
[BB][000001][TASK][BID] source=dog_a task=find_dog_001 decision=accept score=1.00
[BB][000002][TASK][BID] source=dog_b task=find_dog_001 decision=accept score=0.50
[BB][000003][TASK][TASK_ASSIGNED] source=coord task=find_dog_001 owner=dog_a
[BB][000004][RECEIPT][RECEIPT] source=dog_a intent=mv-1 success=true
[BB][000005][HUMAN][AUTH_POINT] source=safety intent=enter-1 status=pending
[BB][000006][HUMAN][AUTH_DECISION] source=human intent=enter-1 approved=true
[BB][000007][RECEIPT][CLAIM] source=dog_a resource=passage_X granted=true
[BB][000008][EVIDENCE][CLUE] source=dog_a clue=clue-1 confidence=0.60
[BB][000009][TASK][TASK_REPLAN] source=coord task=find_dog_001 reason=new_clue
[BB][000010][RECEIPT][ESTOP] source=estop-bus scope=all state=activated
[BB][000011][TASK][TASK_DONE] source=coord task=find_dog_001 success=true
```

### 15.4 当前状态摘要

关键事件发生后显示：

```text
=== BLACKBOARD VIEW ==================================================
TASKS
  find_dog_001  status=assigned  owner=dog_a  mode=autonomous

BIDS
  find_dog_001  dog_a=1.00/accept  dog_b=0.50/accept

EVIDENCE
  clue-1  task=find_dog_001  pos=waypoint_B  confidence=0.60

AUTHORIZATION
  enter-1  approved  approver=指挥官

LEASES
  passage_X  holder=dog_a  status=active

SAFETY
  scope=all  estop=false
======================================================================
```

### 15.5 监视器约束

- 监视器只能读事件和视图，不能产生业务事件。
- 展示失败不能中断 `Blackboard.append()`。
- MVP 可以直接 print；为避免 handler 阻塞，建议使用标准库 `queue.Queue + daemon Thread`。
- JSONL 一行保存一个原始事件，用于运行后复盘，但不作为正式事实存储。
- 终端只展示最近事件，完整事件仍由黑板/Trace 保存。

### 15.6 终端订阅接线

```python
monitor = ConsoleBlackboardMonitor(blackboard=h.blackboard)
h.blackboard.subscribe(monitor.on_event)
```

推荐 Trace 先订阅，Monitor 后订阅。Monitor 不应依赖自己是第几个订阅者。

## 16. MVP 1.0 实施范围

### 16.1 必须实现

1. TASK_POSTED 保存完整 TaskPackage 字段。
2. `read_events()` 支持 task_id 过滤。
3. BID 使用统一字段和幂等键。
4. TASK_ASSIGNED 使用统一字段和幂等键。
5. 所有执行路径统一写完整 RECEIPT。
6. R1 授权保存 AUTH_POINT/AUTH_DECISION，批准后才执行。
7. claim 成功和失败都写 CLAIM；真正产生 YIELD。
8. 集中式任务也先写 TASK_POSTED。
9. CLUE 保存完整位置、来源和置信信息。
10. 每个 CLUE 只触发一次 TASK_REPLAN。
11. ESTOP 激活/解除进入 safety 视图；失败回执保留原因。
12. TASK_DONE 保存成功结果和证据。
13. `fold_view()` 补 tasks/bids/evidence/receipts/human/leases/safety/replans。
14. ConsoleBlackboardMonitor 展示事件流和当前状态。
15. 为上述流程增加自动化测试。

### 16.2 可以保留的简单实现

- `_events: list`
- `_seen_keys: set`，但重复事件必须正确拦截
- 同步 `subscribe()`
- 全量 `fold_view()`
- `_leases: dict`
- 电量直接作为 bid_score
- mock 人工授权
- mock Adapter
- 普通 print 终端输出

### 16.3 MVP 暂不实现

- 数据库。
- 多进程。
- 消费者 ACK 和死信队列。
- 租约 TTL 和 fencing token。
- 真正异步竞价窗口。
- 真 LLM 重规划。
- 线索融合算法。
- Skill 事件化和自进化。
- 前端和外部接口。

## 17. 三个主要模块的交付边界

### 17.1 入口层负责人

交付：

```text
raw_input → TaskPackage → TASK_POSTED
```

必须保证：

- task_id 唯一稳定。
- TASK_POSTED 字段完整。
- mode、requirement、priority 类型正确。
- 重试使用同一 idempotency_key。

### 17.2 黑板负责人

交付：

```text
append/read_events/subscribe/query_view/claim/release
事件去重、offset/version、派生视图、终端监视
```

必须保证：

- 不解释或修改业务 payload。
- 按 task_id 隔离数据。
- 同一事件不重复保存。
- 所有订阅者收到约定类型。
- 事件和当前视图一致。

### 17.3 协同层负责人

交付：

```text
TASK_POSTED → BID
BID[] → TASK_ASSIGNED
CLUE → TASK_REPLAN
ActionReceipt → RECEIPT
```

必须保证：

- 只处理当前任务数据。
- BID/TASK_ASSIGNED/RECEIPT 字段符合契约。
- 不直接修改黑板内部字典。
- 不依赖所有 BID 永远同步到达；MVP 测试可固定同步，2.0 改竞价窗口。

## 18. MVP 集成验收

### 18.1 链路一

预期事件：

```text
TASK_POSTED
BID dog_a
BID dog_b
TASK_ASSIGNED dog_a
RECEIPT mv-1
```

断言：

- 任务字段完整。
- 两个 BID 都属于当前 task_id。
- dog_a 中标。
- view.owner == dog_a。
- RECEIPT 包含完整结果。

### 18.2 链路二

预期事件：

```text
AUTH_POINT
AUTH_DECISION
RECEIPT enter-1
CLAIM dog_a granted=true
CLAIM dog_b granted=false
YIELD dog_b
```

断言：未经批准不执行；授权执行有回执；资源只有一个持有者。

### 18.3 链路三

预期事件：

```text
TASK_POSTED find_dog_002 mode=master_worker
TASK_ASSIGNED find_dog_002 assignment_mode=master_worker
```

断言：第二个任务在 view 中有完整 content，而不是 assigned 空壳。

### 18.4 链路四

预期事件：

```text
CLUE clue-1
TASK_REPLAN reason=new_clue
```

断言：每个 Agent 对 clue-1 只重评估一次，不重复触发。

### 18.5 收尾

预期事件：

```text
ESTOP activated
RECEIPT after-estop success=false/failure_reason=device_estopped
ESTOP cleared
TASK_DONE
```

断言：最终 task_view=done，safety_view.estop=false，Trace 事件数与黑板事件数一致。

## 19. 2.0 优化架构

### 19.1 事件信封升级

2.0 将 task_id、版本和因果关系上提到信封：

```python
@dataclass(frozen=True)
class BlackboardEventV2:
    event_id: str
    event_type: EventType
    ledger: Ledger
    task_id: str | None
    aggregate_id: str
    aggregate_version: int
    source: str
    payload: Mapping[str, Any]
    occurred_at: datetime
    idempotency_key: str
    schema_version: int = 1
    correlation_id: str | None = None
    causation_id: str | None = None
    confidence: float | None = None
    expires_at: datetime | None = None
```

优化点：

- 不再从任意 content 中猜 task_id。
- 事件不可变。
- 每个聚合有独立版本。
- 能还原 TASK_POSTED → BID → ASSIGNED → RECEIPT 的因果链。
- 旧事件通过 schema_version/upcaster 兼容。

### 19.2 BlackboardPort 升级

```python
append(event, expected_version=None) -> AppendResult
append_batch(events) -> list[AppendResult]
read_events(EventQuery) -> list[StoredEvent]
subscribe(consumer_name, types, start_policy) -> subscription_id
ack(consumer_name, offset)
nack(consumer_name, offset, reason)
replay(projection_name)
claim(ClaimRequest) -> LeaseResult
renew(LeaseRenewRequest) -> LeaseResult
release(LeaseReleaseRequest) -> LeaseResult
```

重复 append 返回原事件 offset 和 `duplicate=True`。

### 19.3 异步可靠分发

```text
append 只提交事件
→ dispatcher 独立读取
→ 每个消费者有自己的 offset
→ handler 成功 ACK
→ 失败重试
→ 超过次数进入 DLQ
```

一个 Trace 错误不能阻塞 AgentLoop；一个 Agent 错误不能阻塞其他 Agent。

### 19.4 PostgreSQL 唯一事实源

2.0 推荐 PostgreSQL 保存：

```text
bb_events
bb_aggregate_heads
bb_consumer_offsets
bb_projection_offsets
bb_task_view
bb_evidence_view
bb_receipt_view
bb_authorization_view
bb_safety_view
bb_leases
bb_dead_letters
```

关键技术：

- `BIGINT IDENTITY` 作为全局 offset。
- `UNIQUE(idempotency_key)` 保证并发去重。
- `UNIQUE(aggregate_id, aggregate_version)` 保证聚合版本。
- `JSONB` 保存不同事件 payload。
- `SELECT ... FOR UPDATE` 原子处理租约和版本头。
- replay 重建所有投影。

PostgreSQL 的 `INSERT ... ON CONFLICT` 可用于并发幂等写入；JSONB 支持索引；行锁适合原子租约实现：

- [PostgreSQL INSERT](https://www.postgresql.org/docs/current/sql-insert.html)
- [PostgreSQL JSONB](https://www.postgresql.org/docs/current/datatype-json.html)
- [PostgreSQL 行锁](https://www.postgresql.org/docs/current/explicit-locking.html)

### 19.5 资源租约升级

2.0 LeaseResult：

```python
LeaseResult(
    acquired=True,
    resource_id="passage_X",
    holder_id="dog_a",
    task_id="find_dog_001",
    fencing_token=7,
    expires_at=...,
    event_offset=...,
)
```

支持：

- TTL。
- 续租。
- 自动过期。
- holder 离线回收。
- fencing token 阻止旧持有者继续使用资源。
- claim/release 与事件写入同一事务。

### 19.6 多进程消息分发

只有 AgentLoop、Trace、投影器拆成多进程后才引入 NATS JetStream。

```text
PostgreSQL bb_events + bb_outbox
→ OutboxPublisher
→ NATS JetStream
→ durable consumers
```

采用 at-least-once + 消费端幂等，不依赖所谓端到端 exactly-once。JetStream 支持 durable consumer、ACK 和重投递：[NATS JetStream Consumers](https://docs.nats.io/nats-concepts/jetstream/consumers)

### 19.7 视图和 ContextAssembler

2.0 使用增量投影，不再每次全量扫描全部事件。

ContextAssembler 按 task_id 读取：

```text
task_view
evidence_view
receipt_view
lease_view
authorization_view
设备 Registry 状态
```

再向 AgentLoop 输出真正可决策的 context。

### 19.8 EventIngress

2.0 实现：

```text
设备故障/Tool 线索/环境摄像头告警
→ EventIngress.ingest(raw)
→ 标准 BlackboardEventV2[]
→ 黑板
```

所有输入都做 schema 校验、来源认证和幂等处理。

### 19.9 2.0 终端 TUI

前端仍可不做。终端监视器升级为 Rich Live Dashboard：

```text
┌ Tasks ───────────────┬ Recent Events ──────────────────┐
│ task/status/owner    │ offset/type/source/summary      │
├ Evidence ────────────┼ Consumers ──────────────────────┤
│ clue/confidence/ttl  │ consumer/offset/lag/error       │
├ Leases ──────────────┼ Safety/Auth ────────────────────┤
│ holder/token/expires │ estop/pending approvals         │
└──────────────────────┴──────────────────────────────────┘
```

Rich 支持终端 Table、Layout、JSON 和 Live 更新：[Rich 官方文档](https://rich.readthedocs.io/en/stable/)

## 20. 从 MVP 迁移到 2.0

| 能力 | MVP 1.0 | 2.0 |
|---|---|---|
| 存储 | 内存 list/dict | PostgreSQL |
| 事件结构 | dataclass + content dict | frozen envelope + typed payload |
| task_id | content 内字段 | 信封一等字段 |
| 版本 | 全局递增整数 | aggregate_version + expected_version |
| 幂等 | 内存 seen_keys | 数据库唯一约束 |
| 订阅 | 同步 callback | dispatcher + cursor + ACK/重试 |
| 派生视图 | 每次全量 fold | 增量物化投影 |
| 重启恢复 | 无 | replay + checkpoint |
| 租约 | dict | 事务 + TTL + fencing token |
| 新线索 | mock 手工写 | EventIngress 标准化 |
| 展示 | 标准库终端日志 | Rich Live TUI + JSONL |
| 消息总线 | 无 | 多进程后可选 NATS JetStream |

迁移原则：MVP 冻结的事件含义和字段不能随意变化。2.0 可以升级信封和存储，但 TASK_POSTED、BID、TASK_ASSIGNED、RECEIPT 等业务含义应保持兼容。

## 21. 推荐实施顺序

### MVP 1.0

```text
1. 三方共同确认本文件字段
2. 修改 contracts
3. 入口层输出完整 TASK_POSTED
4. 黑板实现 task_id 过滤和完整 fold_view
5. 协同层输出规范 BID/TASK_ASSIGNED/RECEIPT
6. 补安全、租约、线索和完成事件
7. 接 ConsoleBlackboardMonitor
8. 用完整 skeleton 做集成测试
9. 契约标记为 v1.0 并冻结
```

### 2.0

```text
1. 事件信封 v2 + typed payload
2. dispatcher/ACK/retry/replay
3. PostgreSQL Event Store
4. 增量投影和 ContextAssembler
5. 事务租约和状态恢复
6. EventIngress
7. Rich TUI
8. 多进程后按需增加 NATS JetStream
```

## 22. MVP 完成定义

满足以下条件，黑板 MVP 才算完成：

- 架构图中 ingress、coordination、safety、access、assets 与黑板的连接均有明确字段契约。
- 完整 skeleton 的协同事实都有事件，不再只 print。
- 多任务不会串 BID、CLUE、RECEIPT 或状态。
- 所有关键事件有稳定 idempotency_key。
- query_view 能展示任务、报价、证据、回执、授权、租约、安全和重规划。
- 每个 CLUE 只触发一次重规划。
- 集中式任务和授权动作不会漏记。
- 终端可以实时看到事件流和当前状态。
- 四条链路均有自动化测试。
- 三个主要模块只依赖 contracts，不依赖彼此内部实现。
