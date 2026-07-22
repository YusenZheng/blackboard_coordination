# 黑板模块技术文档

## 1. 黑板是什么

在“群智大脑”系统里，黑板是所有 Agent 共用的信息中心。

可以把它理解成：

- 公共任务板：记录当前有什么任务、任务进展到哪一步。
- 公共记事本：记录谁发现了什么线索、谁执行了什么动作。
- 公共账本：记录历史过程，方便复盘、评测和追踪问题。
- 资源占用表：记录某个区域、通道、设备能力当前被谁占用，避免多个 Agent 冲突。

系统设计要求成员之间不直接点对点通信。无人机 Agent、无人车 Agent、机器狗 Agent 都通过黑板读写信息来协作。

一句话：

> 黑板负责让所有 Agent 看到同一个事实版本。

## 2. 黑板在系统里的位置

根据架构文档，系统大致分成这些层：

- `ingress`：人机入口，把自然语言任务变成结构化任务。
- `coordination`：协同层，负责 Agent 决策、任务分配、冲突处理。
- `blackboard`：黑板层，负责共享事件、当前状态视图和资源租约。
- `access`：接入层，把统一动作翻译成真实设备或仿真设备指令。
- `safety`：安全平面，对动作进行放行、拦截或人工授权。
- `assets`：资产层，记录 Trace、沉淀 Skill、做复盘。
- `contracts`：契约层，定义所有层共同遵守的数据结构。

黑板位于协同核心位置：

```text
人输入任务
  ↓
ingress 生成任务包
  ↓
blackboard 写入任务事件
  ↓
coordination 中的 Agent Loop 读取黑板并决策
  ↓
safety 检查动作
  ↓
access 下发到设备
  ↓
执行结果再写回 blackboard
```

## 3. 当前代码实现概览

当前代码中的黑板模块在：

```text
blackboard/
  board.py
  view.py
```

相关契约在：

```text
contracts/
  blackboard_event.py
  interfaces.py
```

当前实现是一个 MVP，也就是最小可运行版本：

- 使用内存列表保存事件。
- 使用 append-only 方式追加事件。
- 支持读取事件历史。
- 支持从事件流折叠出当前视图。
- 支持订阅事件。
- 支持简单资源租约 `claim/release`。
- 暂时没有接数据库、Redis、Kafka 等持久化或消息中间件。

也就是说，现在这个黑板适合验证流程，但还不是生产级实现。

## 4. 当前核心类

### 4.1 `Blackboard`

位置：

```text
blackboard/board.py
```

它是当前黑板的主类，核心职责有四个：

```text
append(event)      写入事件
read_events(...)   读取历史事件
query_view(...)    查询当前状态视图
subscribe(...)     订阅新事件
claim/release      申请和释放资源租约
```

当前内部数据结构：

```python
self._events = []
self._seen_keys = set()
self._subscribers = []
self._leases = {}
```

通俗解释：

- `_events`：保存所有黑板事件。
- `_seen_keys`：防止同一个事件重复写入。
- `_subscribers`：保存订阅者，比如 Trace、Agent Loop。
- `_leases`：保存资源占用关系，比如 `passage_X -> dog_a`。

### 4.2 `BlackboardEvent`

位置：

```text
contracts/blackboard_event.py
```

这是黑板事件的数据结构。

一条事件大概长这样：

```python
BlackboardEvent(
    id="clue-1",
    type=EventType.CLUE,
    ledger=Ledger.EVIDENCE,
    content={
        "clue_id": "clue-1",
        "task_id": "task-001",
        "position": "waypoint_B"
    },
    source="dog_a",
    confidence=0.6
)
```

通俗解释：

- `id`：事件编号。
- `type`：事件类型，比如任务发布、线索发现、动作回执。
- `ledger`：这条事件属于哪个账本。
- `content`：具体内容，用字典保存。
- `source`：是谁写入的，比如 `ingress`、`dog_a`、`coordination`。
- `confidence`：置信度，可选。
- `version`：版本号，用于防止状态混乱。
- `idempotency_key`：幂等键，用于防止重复写入。

### 4.3 `fold_view`

位置：

```text
blackboard/view.py
```

黑板保存的是历史事件，但 Agent 和前台页面通常关心的是“当前状态”。

所以 `fold_view` 的作用就是：

```text
把一串历史事件折叠成当前状态
```

比如事件历史是：

```text
任务 task_001 被发布
任务 task_001 分配给 dog_a
任务 task_001 完成
```

折叠后的当前视图就是：

```text
task_001 当前状态 = done
task_001 当前负责人 = dog_a
```

## 5. 黑板事件为什么要 append-only

append-only 的意思是：

```text
事件一旦写入，就不修改、不删除。
状态变化靠追加新事件表达。
```

比如不要直接把任务状态从 `posted` 改成 `done`，而是追加：

```text
TASK_POSTED
TASK_ASSIGNED
TASK_DONE
```

这样设计有几个好处：

- 可以完整复盘任务过程。
- 可以追踪谁在什么时候做了什么。
- 可以让 Trace、评测、运营账本从同一个事实源取数。
- 可以在系统重启后通过 replay 重建当前状态。
- 可以减少并发更新时互相覆盖的问题。

## 6. 四类账本

当前代码里定义了四类 Ledger：

```python
class Ledger(str, Enum):
    TASK = "task"
    EVIDENCE = "evidence"
    RECEIPT = "receipt"
    HUMAN = "human"
```

通俗解释：

| 账本 | 作用 | 例子 |
|---|---|---|
| `TASK` | 记录任务状态 | 任务发布、任务分配、任务完成 |
| `EVIDENCE` | 记录事实和线索 | 发现疑似目标、线索融合 |
| `RECEIPT` | 记录动作执行结果 | 移动成功、执行失败、资源占用 |
| `HUMAN` | 记录人工确认 | 授权请求、同意、拒绝 |

为什么要分账本？

因为不同信息的用途不同：

- 任务账本给任务流转用。
- 证据账本给判断和搜索用。
- 回执账本给执行追踪用。
- 人类账本给安全授权和审计用。

## 7. 当前支持的事件类型

代码里的 `EventType` 包括：

### 7.1 任务类

```text
TASK_POSTED      任务发布
TASK_ASSIGNED    任务分配
TASK_DONE        任务完成
TASK_REPLAN      任务重规划
```

### 7.2 协同类

```text
BID              Agent 应征出价
CLAIM            申请占用资源
YIELD            主动让位
HANDOFF          移交任务或目标
ASK_HELP         请求协助
```

### 7.3 证据类

```text
CLUE             上报线索
FUSION           线索融合结果
```

### 7.4 人工确认类

```text
AUTH_POINT       请求人工授权
AUTH_DECISION    人工授权结果
```

### 7.5 安全与回执类

```text
RECEIPT          动作执行回执
SAFETY_INTERCEPT 安全拦截
ESTOP            急停事件留痕
```

### 7.6 资产类

```text
SKILL_REGISTERED 技能注册
```

## 8. 黑板当前工作流程

以“寻找走失犬只”为例。

### 8.1 任务发布

`ingress` 生成任务后，向黑板写入：

```text
TASK_POSTED
```

事件内容示例：

```json
{
  "task_id": "task_001",
  "goal": "寻找走失犬只"
}
```

### 8.2 Agent 看到任务后应征

每个 Agent Loop 订阅黑板事件。

当它收到 `TASK_POSTED` 后，会判断自己是否适合接任务，然后写入：

```text
BID
```

事件内容示例：

```json
{
  "task_id": "task_001",
  "device_id": "dog_a",
  "decision": "accept",
  "bid_score": 0.82,
  "reason": "电量足够，适合地面搜索"
}
```

### 8.3 协同层选择中标者

`coordination.conflict.ConflictService.rank_bids` 会读取所有 `BID` 事件，选择分数最高的 Agent。

然后写入：

```text
TASK_ASSIGNED
```

### 8.4 Agent 执行动作

中标 Agent 产生动作意图，经安全检查后通过工具网关执行。

执行结束后写入：

```text
RECEIPT
```

表示动作成功或失败。

### 8.5 执行过程中发现线索

设备或 Agent 发现新线索时，写入：

```text
CLUE
```

比如：

```json
{
  "clue_id": "clue-1",
  "task_id": "task_001",
  "position": "waypoint_B",
  "description": "发现疑似目标"
}
```

其他 Agent 看到线索后，可以重新评估任务计划。

### 8.6 任务完成

任务结束时写入：

```text
TASK_DONE
```

当前视图里任务状态变成：

```text
done
```

## 9. 订阅机制

当前 `Blackboard.subscribe` 支持订阅黑板事件。

典型订阅者：

- `Trace`：订阅所有事件，用于留档和复盘。
- `AgentLoop`：订阅任务、线索等事件，用于驱动协作决策。

当前代码中，`runtime.harness.Harness` 会执行：

```python
self.blackboard.subscribe(self.trace.on_event)
self.blackboard.subscribe(loop.on_event)
```

通俗解释：

```text
黑板有新事件
  ↓
马上通知 Trace 记录下来
  ↓
马上通知各个 Agent Loop 看要不要行动
```

当前订阅是同步回调，也就是在同一个 Python 进程里直接调用函数。

未来生产版建议改为：

```text
Redis Stream / Kafka / NATS / PostgreSQL LISTEN-NOTIFY
```

这样可以支持多进程、多机器、断线重连和消费 offset。

## 10. 当前视图机制

当前视图不是单独手写保存的，而是从事件流计算出来。

代码：

```text
blackboard/view.py -> fold_view(...)
```

当前折叠规则比较简单：

- `TASK_POSTED`：任务状态变成 `posted`。
- `TASK_ASSIGNED`：任务状态变成 `assigned`，记录负责人。
- `TASK_DONE`：任务状态变成 `done`。
- `CLUE`：按 `clue_id` 保存线索。
- `RECEIPT`：按 `intent_id` 保存动作回执。
- `AUTH_POINT` / `AUTH_DECISION`：按 `intent_id` 保存人工授权过程。

查询结果结构：

```python
{
    "tasks": {},
    "evidence": {},
    "receipts": {},
    "human": {}
}
```

通俗理解：

```text
事件流 = 历史录像
当前视图 = 现在这一刻的任务大屏
```

## 11. 租约机制

租约用于解决物理资源冲突。

例如：

```text
窄巷 passage_X 只能允许一台机器狗进入
```

Agent 进入前要先申请：

```python
blackboard.claim("passage_X", "dog_a")
```

如果没有人占用，返回 `True`。

如果已经被其他设备占用，返回 `False`。

如果同一个设备重复申请自己已经持有的资源，也返回 `True`。

释放资源：

```python
blackboard.release("passage_X", "dog_a")
```

当前实现内部是：

```python
self._leases = {
    "passage_X": "dog_a"
}
```

当前版本的限制：

- 租约只存在内存里，程序重启会丢失。
- 没有过期时间。
- 没有自动续约。
- `claim/release` 还没有完全事件化，所以不能完全通过事件 replay 恢复。

生产版建议使用数据库表：

```text
leases
```

字段建议：

```text
id
resource_id
holder_agent_id
task_id
status
expires_at
created_at
updated_at
```

## 12. 幂等与版本

### 12.1 幂等

幂等的意思是：

```text
同一件事重复提交多次，只生效一次。
```

当前 `Blackboard.append` 会检查：

```python
event.idempotency_key
```

如果这个 key 已经出现过，就不重复追加事件。

适用场景：

```text
网络重试
接口超时后重复提交
Agent 重复上报同一条线索
```

### 12.2 版本

当前黑板会给没有版本号的事件自动分配递增版本：

```python
event.version = next(self._version)
```

作用是：

```text
让事件有明确顺序
为未来并发控制做准备
```

当前只是 MVP 版本递增，未来如果接数据库，需要升级为 CAS 或事务控制。

## 13. 当前实现边界

当前黑板已经能跑通 L0 骨架流程，但还不是完整工程化版本。

已具备：

- append-only 事件流。
- 四类账本。
- 幂等去重。
- 版本递增。
- 事件订阅。
- 派生视图。
- 简单资源租约。
- Trace 订阅落档。
- Agent Loop 事件驱动。

暂未具备：

- 数据库持久化。
- 真正并发安全。
- 跨进程订阅。
- 消费 offset 和 ack。
- 租约过期和续约。
- 事件 replay 重建全部状态。
- 前端实时事件流。
- 完整授权表。
- 完整查询过滤。
- 权限控制和审计。

## 14. 推荐的工程化技术方案

如果要把当前内存黑板升级成可用服务，推荐：

```text
Python + FastAPI + PostgreSQL + Redis
```

### 14.1 Python

用于写黑板后端逻辑。

当前项目本身就是 Python，所以继续用 Python 最自然。

### 14.2 FastAPI

FastAPI 用来提供 HTTP 接口。

可以理解成：

```text
给黑板开门，让其他模块通过网址访问黑板
```

建议接口：

```text
POST /blackboard/events
GET  /blackboard/events
GET  /blackboard/view
POST /blackboard/leases/claim
POST /blackboard/leases/release
GET  /blackboard/leases
POST /blackboard/authorizations
POST /blackboard/authorizations/{id}/approve
POST /blackboard/authorizations/{id}/reject
GET  /blackboard/stream
```

### 14.3 PostgreSQL

PostgreSQL 用来永久保存黑板数据。

可以理解成：

```text
黑板的长期档案柜
```

建议保存：

- 黑板事件。
- 当前物化视图。
- 租约。
- 授权请求。
- 订阅消费 offset。

### 14.4 Redis

Redis 用来做实时通知。

可以理解成：

```text
黑板的广播喇叭
```

写入新事件后：

```text
1. 保存到 PostgreSQL
2. 通过 Redis 通知订阅者
```

第一阶段可以不加 Redis，先用 FastAPI + PostgreSQL。

## 15. 推荐数据库表设计

### 15.1 `blackboard_events`

保存所有历史事件。

```text
id                事件唯一 ID
event_type        事件类型
ledger            所属账本
task_id           任务 ID，可为空
agent_id          Agent ID，可为空
source            来源
content           JSON 内容
confidence        置信度
ttl               新鲜度
version           版本号
idempotency_key   幂等键
created_at        创建时间
```

### 15.2 `blackboard_views`

保存当前状态快照，也叫物化视图。

```text
id
view_type         task / agent / evidence / receipt / human
object_id         task_id / agent_id / clue_id / intent_id
data              JSON 当前状态
version
updated_at
```

### 15.3 `leases`

保存资源占用。

```text
id
resource_id
holder_agent_id
task_id
status            active / released / expired
expires_at
created_at
updated_at
```

### 15.4 `authorization_requests`

保存人工授权请求。

```text
id
task_id
agent_id
intent_id
action
reason
risk_level
status            pending / approved / rejected / expired
created_at
decided_at
decided_by
decision_note
```

### 15.5 `event_subscriptions`

保存订阅消费进度。

```text
id
subscriber_name
last_offset
last_event_id
updated_at
```

## 16. 推荐接口设计

### 16.1 写入事件

```text
POST /blackboard/events
```

请求示例：

```json
{
  "id": "clue-1",
  "event_type": "clue",
  "ledger": "evidence",
  "task_id": "task_001",
  "agent_id": "drone_001",
  "source": "drone_001",
  "content": {
    "clue_id": "clue-1",
    "position": "A区草坪",
    "description": "发现疑似白色小狗"
  },
  "confidence": 0.72,
  "idempotency_key": "drone_001:clue-1"
}
```

返回示例：

```json
{
  "ok": true,
  "offset": 128,
  "version": 42
}
```

### 16.2 查询事件

```text
GET /blackboard/events?task_id=task_001
GET /blackboard/events?since_offset=100
GET /blackboard/events?ledger=evidence
```

### 16.3 查询当前视图

```text
GET /blackboard/view
GET /blackboard/view?ledger=task
GET /blackboard/view/tasks/task_001
```

### 16.4 申请资源

```text
POST /blackboard/leases/claim
```

请求示例：

```json
{
  "resource_id": "passage_X",
  "agent_id": "dog_a",
  "task_id": "task_001",
  "ttl_seconds": 300
}
```

返回示例：

```json
{
  "ok": true,
  "resource_id": "passage_X",
  "holder_agent_id": "dog_a",
  "expires_at": "2026-07-20T15:30:00"
}
```

### 16.5 释放资源

```text
POST /blackboard/leases/release
```

请求示例：

```json
{
  "resource_id": "passage_X",
  "agent_id": "dog_a"
}
```

### 16.6 创建授权请求

```text
POST /blackboard/authorizations
```

请求示例：

```json
{
  "task_id": "task_001",
  "agent_id": "dog_a",
  "intent_id": "enter-1",
  "action": "进入封闭区域",
  "reason": "需要确认疑似目标",
  "risk_level": "R1"
}
```

### 16.7 审批授权

```text
POST /blackboard/authorizations/{id}/approve
POST /blackboard/authorizations/{id}/reject
```

### 16.8 实时事件流

```text
GET /blackboard/stream?from_offset=100
```

第一版可以用 Server-Sent Events，后续可升级为 WebSocket 或消息队列。

## 17. 写事件时应该做什么

生产版 `append_event` 建议流程：

```text
1. 校验事件格式
2. 检查 idempotency_key，防止重复写
3. 开启数据库事务
4. 写入 blackboard_events
5. 更新 blackboard_views
6. 如果是租约/授权事件，同步更新对应表
7. 提交事务
8. 发布实时通知
9. 返回 offset/version
```

关键原则：

```text
事件写入和当前视图更新必须在同一个事务里完成。
```

否则可能出现：

```text
事件已经写入，但当前视图没更新
```

或者：

```text
当前视图更新了，但事件历史没有记录
```

## 18. 与其他模块的关系

### 18.1 与 `ingress`

`ingress` 负责把用户任务写入黑板：

```text
TASK_POSTED
```

### 18.2 与 `coordination`

`coordination` 读取任务和线索，写入：

```text
BID
TASK_ASSIGNED
TASK_REPLAN
HANDOFF
ASK_HELP
```

### 18.3 与 `access`

设备执行后的结果写回黑板：

```text
RECEIPT
CLUE
```

### 18.4 与 `safety`

安全拦截和授权点写入：

```text
SAFETY_INTERCEPT
AUTH_POINT
AUTH_DECISION
ESTOP
```

### 18.5 与 `assets.Trace`

Trace 订阅黑板事件，用于长期归档和复盘。

注意：

```text
Trace 是黑板事件的订阅者，不应该和黑板双写同一份协同事实。
```

黑板是协同事实的主源。

## 19. 第一阶段开发目标

如果只负责黑板，建议第一阶段目标是：

- 保持现有 `BlackboardPort` 接口不变。
- 把内存事件流换成数据库事件表。
- 保留 `append/read_events/query_view/subscribe/claim/release` 语义。
- 增加 FastAPI 接口。
- 增加租约过期时间。
- 增加授权请求表。
- 增加基本测试。

第一阶段不必做：

- Kafka。
- 多机部署。
- 复杂权限系统。
- 完整前端大屏。
- 真机接入。

## 20. 建议迭代路线

### 阶段 0：当前内存版

目标：

```text
跑通 L0 walking skeleton
```

已完成：

- 内存事件流。
- 派生视图。
- 订阅。
- 简单租约。

### 阶段 1：服务化黑板

目标：

```text
让黑板变成可被外部调用的后端服务
```

实现：

- FastAPI 接口。
- PostgreSQL 持久化。
- 事件表。
- 当前视图表。
- 租约表。
- 授权表。

### 阶段 2：实时通知

目标：

```text
Agent 和前台能实时收到新事件
```

实现：

- SSE 或 WebSocket。
- Redis Pub/Sub 或 Redis Stream。
- 订阅 offset。

### 阶段 3：并发与恢复

目标：

```text
支持多个 Agent 同时写入，系统重启后能恢复状态
```

实现：

- 数据库事务。
- 乐观锁/CAS。
- 事件 replay。
- 租约过期和续约。
- 消费 ack。

### 阶段 4：生产级能力

目标：

```text
支持真实多设备、多任务、高可靠协作
```

实现：

- 权限控制。
- 审计日志。
- 指标监控。
- 告警。
- 消息队列。
- 分布式部署。

## 21. 最小可交付清单

黑板模块第一版可交付：

- `BlackboardEvent` 事件 schema。
- `blackboard_events` 事件表。
- `blackboard_views` 当前视图表。
- `leases` 租约表。
- `authorization_requests` 授权表。
- `append_event` 写事件逻辑。
- `fold_view` 或物化视图更新逻辑。
- `claim/release` 租约逻辑。
- `subscribe/stream` 事件订阅逻辑。
- FastAPI 接口文档。
- 单元测试和一条找狗任务示例流程。

## 22. 小白版总结

黑板不是一个普通页面，而是整个多 Agent 系统的公共事实中心。

它要解决四个问题：

```text
大家现在要干什么？
谁在哪里，正在干什么？
发现了什么新线索？
哪些资源已经被占用，哪些动作需要人确认？
```

技术上，它应该做成：

```text
事件表 + 当前视图 + 租约表 + 授权表 + 实时通知
```

当前代码已经实现了内存版骨架。后续工程化时，建议升级为：

```text
FastAPI + PostgreSQL + Redis
```

其中：

- FastAPI 负责提供接口。
- PostgreSQL 负责永久保存事件和状态。
- Redis 负责实时通知。
- 黑板事件是所有协作事实的唯一写入源。

