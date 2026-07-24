# 基于 OxyGent 的黑板模块创新借鉴方案

## 1. 背景

当前 `swarm_brain` 项目的黑板模块已经具备 MVP 能力：可以追加事件、读取事件、生成派生视图、通知订阅者，并通过租约机制处理关键资源占用。

但当前黑板仍然偏向“事件列表”：

- 事件之间的因果关系还不明显。
- 所有事件基本处在同一层空间里。
- Trace 归档后，哪些数据有复盘价值、哪些数据可进入经验库，还没有质量管理。
- 黑板还没有形成完整的运行轨迹图。
- 对事件拥堵、订阅耗时、资源抢占频率等运行指标还缺少观测。

OxyGent 论文中提出的几个机制，对黑板模块很有参考价值：统一抽象、分层数据作用域、运行时执行图、标准生命周期、Trace 回流、质量门控和性能观测。

本文件不要求直接照搬 OxyGent，而是结合当前项目的黑板设计，提炼出适合落地的增强方向。

## 2. 总体目标

黑板不应该只是“保存消息的地方”，而应该升级为：

> 多 Agent 协作过程中的可观察运行轨迹层。

也就是说，黑板不仅要回答：

```text
发生了什么？
```

还要进一步回答：

```text
这件事是谁触发的？
属于哪个任务范围？
影响了哪个 Agent？
后续产生了哪些动作？
这个事件有没有复盘价值？
是否能沉淀成经验？
哪里出现了资源拥堵？
```

最终黑板应该支持：

- 事件隔离。
- 事件追踪。
- 因果链路还原。
- 当前状态查询。
- 任务过程复盘。
- 质量审核。
- 经验回流。
- 运行指标观测。

## 3. 借鉴点一：分层数据作用域

### 3.1 OxyGent 启发

OxyGent 将数据分成多层作用域，例如全局级、会话组级、请求级和节点级。这样可以避免所有数据都堆在一个全局状态里，减少数据污染和权限泄露。

### 3.2 黑板中的问题

当前黑板中的事件基本都进入同一个 `_events` 列表。虽然事件中有 `ledger` 可以区分任务、证据、回执、人类确认，但它还不够表达“数据属于哪个范围”。

比如：

```text
系统级设备注册信息
某个 task_id 下的任务过程
某个 Agent 自己的局部判断
某一次动作 intent_id 的执行上下文
```

这些数据不应该完全混在同一层语义里。

### 3.3 建议设计

给 `BlackboardEvent` 增加作用域字段：

```python
scope: str = "task"
scope_id: Optional[str] = None
```

建议作用域：

```text
application：系统级，全局共享
task：任务级，同一个 task_id 下共享
agent：Agent 级，某个 Agent 的局部事件
action：动作级，某个 intent_id 的局部过程
resource：资源级，某个 resource_id 的占用与冲突过程
```

示例：

```python
BlackboardEvent(
    id="post-task-001",
    type=EventType.TASK_POSTED,
    ledger=Ledger.TASK,
    scope="task",
    scope_id="task_001",
    content={"task_id": "task_001", "goal": "寻找走失犬只"},
    source="ingress"
)
```

### 3.4 使用技术

MVP 阶段：

- Python dataclass 扩展字段。
- 内存过滤：`query_view(scope="task", scope_id="task_001")`。

服务化阶段：

- PostgreSQL 字段：

```sql
scope TEXT NOT NULL DEFAULT 'task',
scope_id TEXT
```

- 为查询建立索引：

```sql
CREATE INDEX idx_blackboard_events_scope ON blackboard_events(scope, scope_id);
```

## 4. 借鉴点二：事件因果链与运行图

### 4.1 OxyGent 启发

OxyGent 强调运行时生成实际执行图，而不是只依赖静态工作流。这样开发者可以看到每一步是谁调用了谁、哪个节点触发了哪个节点。

### 4.2 黑板中的问题

当前黑板可以看到事件顺序，但不容易看出事件之间的因果关系。

比如：

```text
TASK_POSTED 触发了 BID
BID 触发了 TASK_ASSIGNED
TASK_ASSIGNED 触发了 ActionIntent
ActionIntent 触发了 RECEIPT
CLUE 触发了 REPLAN
```

现在这些关系主要靠人脑从事件内容里猜。

### 4.3 建议设计

给 `BlackboardEvent` 增加事件关联字段：

```python
parent_event_id: Optional[str] = None
caused_by_event_id: Optional[str] = None
correlation_id: Optional[str] = None
```

字段含义：

```text
parent_event_id：上一级事件，用来形成树状结构
caused_by_event_id：直接触发当前事件的事件
correlation_id：同一条任务链路的统一追踪 ID
```

示例：

```text
TASK_POSTED: post-task_001
  ├─ BID: bid-task_001-dog_a
  ├─ BID: bid-task_001-dog_b
  └─ TASK_ASSIGNED: assign-task_001
        └─ RECEIPT: rcpt-mv-1
```

对应事件：

```python
BlackboardEvent(
    id="bid-task_001-dog_a",
    type=EventType.BID,
    parent_event_id="post-task_001",
    caused_by_event_id="post-task_001",
    correlation_id="task_001",
    ...
)
```

### 4.4 使用技术

MVP 阶段：

- 在 `BlackboardEvent` dataclass 中加字段。
- 写一个简单函数：

```python
def build_event_graph(events: list[BlackboardEvent]) -> dict:
    ...
```

输出结构：

```python
{
    "post-task_001": ["bid-task_001-dog_a", "bid-task_001-dog_b"],
    "assign-task_001": ["rcpt-mv-1"]
}
```

服务化阶段：

- PostgreSQL 增加字段：

```sql
parent_event_id TEXT,
caused_by_event_id TEXT,
correlation_id TEXT
```

- 建索引：

```sql
CREATE INDEX idx_blackboard_events_correlation ON blackboard_events(correlation_id);
CREATE INDEX idx_blackboard_events_parent ON blackboard_events(parent_event_id);
```

可视化阶段：

- 后端输出 Mermaid / JSON graph。
- 前端后续可用 React Flow、D3、Cytoscape.js 展示。

## 5. 借鉴点三：标准化 append 生命周期

### 5.1 OxyGent 启发

OxyGent 将每个节点的执行拆成标准生命周期：执行前处理、保存输入、核心执行、后处理、格式化输出。这样可以把监控、安全、审计等横切逻辑插入固定位置。

### 5.2 黑板中的问题

当前 `Blackboard.append` 已经做了几件事：

```text
幂等检查
分配 version
分配 offset
保存事件
通知订阅者
```

但这些逻辑都放在一个函数里。未来如果再加入：

```text
事件 schema 校验
权限校验
事件 hash
物化视图更新
指标记录
审计日志
异常处理
```

`append` 会变得越来越难维护。

### 5.3 建议设计

将 `append` 拆成标准生命周期：

```python
def append(self, event: BlackboardEvent) -> int:
    self._pre_append(event)
    event = self._normalize_event(event)
    offset = self._persist_event(event)
    self._post_append(event, offset)
    self._notify_subscribers(event, offset)
    return offset
```

每一步职责：

```text
_pre_append：校验事件、检查权限、检查幂等
_normalize_event：补齐 ts、version、scope、event_hash
_persist_event：保存事件
_post_append：更新派生视图、更新指标
_notify_subscribers：通知 Trace / AgentLoop
```

### 5.4 使用技术

MVP 阶段：

- Python 方法拆分。
- 仍然使用内存 `_events`。

服务化阶段：

- PostgreSQL transaction 保证事件保存和视图更新一致。
- SQLAlchemy 管理数据库写入。
- Pydantic 做事件入参校验。

建议技术：

```text
Pydantic：校验事件字段
SQLAlchemy：数据库事务
Python logging：审计日志
pytest：生命周期测试
```

## 6. 借鉴点四：Trace 质量门控与经验回流

### 6.1 OxyGent 启发

OxyBank 不会把所有原始 Trace 都直接变成知识。它会经过去重、标注、审核和批准，只有高质量数据才进入知识库或训练流程。

### 6.2 黑板中的问题

当前 Trace 订阅黑板事件后，只是简单保存：

```python
self._events.append((offset, event))
```

但还没有区分：

```text
哪些事件只是普通日志
哪些事件值得复盘
哪些任务链路失败了
哪些过程能沉淀成 Skill
哪些数据应该被拒绝
```

### 6.3 建议设计

给 Trace 或事件资产增加质量状态：

```text
raw：原始事件
pending_review：待审核
annotated：已标注
approved：已批准
rejected：已拒绝
archived：已归档
```

建议新增一个事件资产表或 TraceAsset 对象：

```python
@dataclass
class TraceAsset:
    asset_id: str
    correlation_id: str
    event_ids: list[str]
    status: str = "raw"
    tags: list[str] = field(default_factory=list)
    score: Optional[float] = None
    reviewer: Optional[str] = None
    notes: str = ""
```

### 6.4 使用技术

MVP 阶段：

- 先在 `assets/trace.py` 中增加简单状态字段。
- 对一次任务链路按 `correlation_id` 聚合。

服务化阶段：

- PostgreSQL 表：

```sql
trace_assets (
  asset_id TEXT PRIMARY KEY,
  correlation_id TEXT,
  event_ids JSONB,
  status TEXT,
  tags JSONB,
  score DOUBLE PRECISION,
  reviewer TEXT,
  notes TEXT,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
)
```

后续可与 `assets/skill.py` 连接：

```text
approved TraceAsset → 萃取 SkillCandidate → AdmissionGate → SkillGraph
```

## 7. 借鉴点五：事件 hash 去重

### 7.1 OxyGent 启发

OxyBank 使用基于 MD5 的去重，避免重复轨迹造成频率偏差。

### 7.2 黑板中的问题

当前黑板已经有：

```python
idempotency_key
```

但它依赖调用方主动传入。如果调用方忘记传，重复内容仍然可能进入黑板。

例如：

```text
无人机连续上报同一个位置的同一条疑似目标
```

如果没有内容级去重，黑板会出现大量重复线索。

### 7.3 建议设计

新增事件内容 hash：

```python
event_hash: Optional[str] = None
```

生成逻辑：

```python
event_hash = md5(
    event.type + event.source + normalized_json(event.content)
)
```

去重策略：

```text
同 event_hash + 同 source + 短时间窗口内重复 → 视为重复事件
```

注意：

```text
不能所有事件都强去重。
BID、RECEIPT、AUTH_DECISION 可以严格去重。
CLUE 可以在时间窗口内去重。
遥测类事件不应该全部去重，因为它本身就是高频变化。
```

### 7.4 使用技术

MVP 阶段：

- Python `hashlib.md5`。
- `json.dumps(content, sort_keys=True)` 做稳定序列化。

服务化阶段：

- PostgreSQL 增加字段：

```sql
event_hash TEXT
```

- 建索引：

```sql
CREATE INDEX idx_blackboard_events_hash ON blackboard_events(event_hash);
```

## 8. 借鉴点六：黑板运行指标观测

### 8.1 OxyGent 启发

OxyGent 强调生产级时间追踪，可以展示任务分布、资源拥堵和执行耗时，帮助优化多 Agent 架构。

### 8.2 黑板中的问题

当前黑板没有记录：

```text
每秒写入多少事件
哪类事件最多
哪个 Agent 最活跃
哪个资源最拥堵
通知订阅者用了多久
某个任务链路耗时多久
```

这些指标对调试多 Agent 协作非常重要。

### 8.3 建议设计

黑板增加轻量指标：

```python
self._metrics = {
    "events_total": 0,
    "events_by_type": {},
    "events_by_source": {},
    "notify_total": 0,
    "notify_errors": 0,
    "leases_claimed": 0,
    "leases_rejected": 0,
}
```

每次 append 更新：

```text
events_total +1
events_by_type[event.type] +1
events_by_source[event.source] +1
```

每次 claim 更新：

```text
claim 成功 → leases_claimed +1
claim 失败 → leases_rejected +1
```

### 8.4 使用技术

MVP 阶段：

- Python dict 记录指标。
- 新增 `blackboard.metrics()` 方法。

服务化阶段：

- Prometheus 指标。
- OpenTelemetry Trace。
- PostgreSQL 聚合查询。

建议指标接口：

```python
def metrics(self) -> dict:
    return copy.deepcopy(self._metrics)
```

未来 HTTP 版：

```text
GET /blackboard/metrics
```

## 9. 推荐优先级

不要一次性全部实现。建议按优先级推进。

### P0：最值得先做

```text
事件作用域 scope / scope_id
事件关联 parent_event_id / caused_by_event_id / correlation_id
```

原因：

```text
这两个能力会直接提升黑板的可读性和可复盘能力。
```

### P1：第二阶段做

```text
append 生命周期拆分
TraceAsset 质量状态
```

原因：

```text
这两个能力会让黑板从“能跑”变成“好维护、可进化”。
```

### P2：后续增强

```text
event_hash 去重
运行指标 metrics
```

原因：

```text
这两个能力更偏工程化和生产观测，可以在基础链路稳定后补。
```

## 10. 建议代码改造草案

### 10.1 扩展 `BlackboardEvent`

建议增加字段：

```python
scope: str = "task"
scope_id: Optional[str] = None
parent_event_id: Optional[str] = None
caused_by_event_id: Optional[str] = None
correlation_id: Optional[str] = None
event_hash: Optional[str] = None
quality_status: str = "raw"
```

### 10.2 拆分 `Blackboard.append`

建议从：

```python
def append(self, event):
    ...
```

拆成：

```python
def append(self, event):
    self._pre_append(event)
    self._normalize_event(event)
    offset = self._persist_event(event)
    self._post_append(event, offset)
    self._notify_subscribers(event, offset)
    return offset
```

### 10.3 增加事件图查询

新增：

```python
def query_event_graph(self, correlation_id: str) -> dict:
    ...
```

用途：

```text
查看某个 task_id 下完整事件链路。
```

### 10.4 增加指标查询

新增：

```python
def metrics(self) -> dict:
    ...
```

用途：

```text
查看黑板运行情况。
```

## 11. 最终效果

完成这些增强后，黑板将从：

```text
一个内存事件列表
```

升级为：

```text
一个有作用域、有因果链、有质量状态、有运行指标的多 Agent 协作轨迹中心
```

它可以支持：

```text
任务复盘
Agent 行为追踪
资源冲突分析
授权过程审计
Trace 资产沉淀
Skill 经验萃取
运行瓶颈定位
```

## 12. 小结

OxyGent 对黑板最重要的启发是：

> 多 Agent 系统需要的不只是消息通信，而是可观察、可复盘、可演化的运行结构。

因此，黑板模块后续的创新方向应该围绕四个关键词展开：

```text
隔离：用 scope 管不同范围的数据
追踪：用 parent/caused_by/correlation 记录因果链
沉淀：用 TraceAsset 和质量状态管理经验回流
观测：用 metrics 发现拥堵、重复和异常
```

