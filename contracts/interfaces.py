"""contracts.interfaces —— 六层接口契约(Protocol/ABC)。

这是"模块之间怎么对话"的核心。每层实现自己那个 Protocol,方法体在实现文件里填;
这里只定签名。每层实现对应 Protocol。

依赖:只 import contracts 自身的数据类型,不 import 任何层实现。
"""
from __future__ import annotations

from typing import Callable, Optional, Protocol

from .agent_card import AgentCard
from .blackboard_event import AppendResult, BlackboardEvent, EventType, Ledger, StoredEvent
from .task import ActionIntent, ActionReceipt, TaskPackage
from .types import DeviceCandidate, ParticipationDecision, TaskRequirement


# ── L1 黑板 ───────────────────────────────────────────────────────────

class BlackboardPort(Protocol):
    """共享黑板:append-only 事件流 + 派生视图。协同唯一介质。

    A12:subscribe 按【异步 + ack/offset】语义设计 —— 即使 MVP 内存实现是同步回调,
    签名也预留 offset/ack,docstring 声明"真上 Kafka/NATS 语义不变",避免协同层
    依赖同步回调、真上中间件时全层改接口。
    """
    def now(self) -> float:
        """返回用于事件时间、竞标截止时间的 Unix epoch 秒。"""
        ...

    def high_watermark(self) -> int:
        """返回已提交事件的全局 version 水位；空流为 0。"""
        ...

    def append(self, event: BlackboardEvent) -> AppendResult:
        """原子追加事件并返回结构化的幂等/冲突结果。"""
        ...

    def query_view(self, ledger: Optional[Ledger] = None,
                   filt: Optional[dict] = None,
                   min_version: Optional[int] = None,
                   timeout_s: float = 0.0) -> dict | list[dict]:
        """读派生的当前状态视图(从事件流折叠,不读全部历史)。"""
        ...

    def read_since(self, offset: int, event_types: Optional[set[str]],
                   limit: int, timeout_s: float) -> list[StoredEvent]:
        """读取严格大于 ``offset`` 的事件；用于 v2 可恢复消费者。"""
        ...

    def read_events(self, since_offset: int = 0,
                    types: Optional[list] = None) -> list:
        """读事件流(复盘/Trace 订阅用)。返回 list[BlackboardEvent]。"""
        ...

    def subscribe(self, handler: Callable[[BlackboardEvent, int], None],
                  types: Optional[list] = None, from_offset: int = 0) -> str:
        """订阅事件(异步语义:handler 收到 (event, offset),处理完可 ack)。
        返回订阅 id。真上中间件时 at-least-once,handler 须幂等。"""
        ...


# ── L5 接入 ───────────────────────────────────────────────────────────

class RegistryPort(Protocol):
    """Agent 化注册处:设备 → AgentCard,能力可注册/发现/刷新,含接入全生命周期。"""
    def register(self, card: AgentCard) -> None:
        """设备接入注册(自注册 MCP server 的抽象;协议实现留下面)。"""
        ...

    def discover(self, requirement: Optional[TaskRequirement] = None) -> list:
        """发现候选设备(能力硬过滤 + online/healthy 过滤)。返回 list[DeviceCandidate]。"""
        ...

    def refresh(self, device_id: str, card: AgentCard) -> None:
        """能力变化(挂新载荷)时刷新(listChanged 抽象)。"""
        ...

    def get(self, device_id: str) -> Optional[AgentCard]: ...

    def all_cards(self) -> list:
        """B14:全部在册 AgentCard(实现已有,契约补齐)。"""
        ...

    def update_resume(self, device_id: str, success: bool) -> None:
        """B14:动作回执 → 履历更新(信用式自治闭环入口)。"""
        ...

    # A3:接入全生命周期(现只有"进",补 heartbeat/离线/注销/重连;实现可留 TODO)
    def heartbeat(self, device_id: str, ts: float) -> None:
        """设备心跳(与遥测通道合流,别开两套存活通道)。TODO 实现。"""
        ...

    def mark_offline(self, device_id: str, reason: str = "") -> None:
        """标记离线(心跳超时/主动下线)→ discover 不再选它。TODO 实现。"""
        ...

    def deregister(self, device_id: str) -> None:
        """设备注销退出。TODO 实现。"""
        ...

    def resume_identity(self, device_id: str) -> Optional[AgentCard]:
        """断线重连恢复身份与上下文(≤30秒,对齐进度指标)。TODO 实现。"""
        ...


class ToolGatewayPort(Protocol):
    """Tool 网关:动作意图 → 设备指令 → 回执。

    A6:dispatch 要容纳五类 Tool(G 设备端 / S 态势云端 / C 协同 / V 社交 / Y 云端),
    不只 G 系列下行。MVP 实现 G + 一个 mock Y,但五类分支都建出。
    """
    def dispatch(self, intent: ActionIntent) -> ActionReceipt:
        """把【已过安全校验】的动作意图翻译成设备指令、收回执。"""
        ...

    def call_tool(self, tool_id: str, params: dict) -> dict:
        """调用一个已注册 Tool(五类之一)。返回结果。"""
        ...


class AdapterPort(Protocol):
    """设备适配器抽象:契约动词 → 具体设备指令。真机/仿真共享此接口(v5 5.9)。"""
    def execute(self, intent: ActionIntent) -> ActionReceipt: ...
    def read_telemetry(self) -> dict: ...


# ── L4 安全 ───────────────────────────────────────────────────────────

class SafetyPort(Protocol):
    """安全平面:三层贯通,拦在每个 tool_call 前后。动作放行须同时过三层。"""
    def check(self, intent: ActionIntent, context: dict = None) -> "SafetyVerdict":
        """pre-tool 校验:语义合规 + 群体安全 + 物理门控(含可逆性 R0/R1/R2)。"""
        ...

    def post_check(self, receipt: ActionReceipt, context: dict = None) -> "SafetyVerdict":
        """post-tool 校验:结果合规性。"""
        ...


class SafetyVerdict(Protocol):
    """安全裁决结果。"""
    allowed: bool          # 放行
    needs_auth: bool       # 需授权点(R1 不可逆动作)
    reason: str


# ── L2 协同 ───────────────────────────────────────────────────────────

class AgentLoopPort(Protocol):
    """每台设备的云端虚拟 Agent 的常驻循环。

    极薄:装 context → LLM 推理 → tool_call → observation 回喂 → 再推理。
    C13:真工程量在 context_assembler 和 budget(电量+时间窗,非 token 计数)。
    """
    def step(self) -> None:
        """跑一轮:感知黑板 → 决策 → (可能)写黑板/调 Tool。"""
        ...

    def on_event(self, event: BlackboardEvent, offset: int = 0) -> None:
        """黑板新事件驱动(应征/让位/接力的决策入口)。

        C2:签名带 offset,与 BlackboardPort.subscribe 的 handler(event, offset) 及
        TracePort.on_event 对齐——board.subscribe 以两参调用所有订阅者,少 offset 会 TypeError。
        """
        ...


class ModeSelectorPort(Protocol):
    """协同模式选择器:按任务标签选挡位(自主应征/master/workflow/单机)。"""
    def route(self, task: TaskPackage) -> str:
        """返回挡位名。MVP 只实现自主应征。"""
        ...


class ConflictPort(Protocol):
    """冲突分治。中性框架服务(租约管理器/bid 排序器,无 LLM),仲裁 Agent 只管 C 类分歧。"""
    def rank_bids(self, task_id: str, decisions: list) -> ParticipationDecision:
        """招投标:对 list[ParticipationDecision] 排序,返回中标者。"""
        ...

    def claim(self, resource: str, device_id: str) -> bool:
        """租约独占:claim 成功返回 True,重叠 claim 被拒返回 False。"""
        ...

    def release(self, resource: str, device_id: str) -> None:
        """yield 释放租约。"""
        ...

    def arbitrate(self, resource: str, contenders: list) -> str:
        """僵局仲裁:三层优先级 P1 安全 > P2 能力 > P3 时间。返回胜出 device_id。"""
        ...

    def debate(self, topic: str, proposals: list) -> dict:
        """B14:C 类方案分歧 —— 有限轮 debate + 加权投票(≤2轮,超时上抛仲裁)。
        实现在 conflict.py 已有(NOT-STAGED),契约补齐签名。"""
        ...


# ── L6 北向入口 ───────────────────────────────────────────────────────

class TaskGenPort(Protocol):
    """任务生成:自然语言/事件 → 结构化任务包。三段 pipeline(A5)。"""
    def generate(self, raw_input: str, media: Optional[list] = None) -> TaskPackage:
        """intent 理解 → enrich 补全(补全 Agent 群) → route 路由打标。"""
        ...


# ── L3 资产 ───────────────────────────────────────────────────────────

class TracePort(Protocol):
    """Trace:订阅黑板事件流派生落档(B2) + 收模型级细节(Trace 独有,不回黑板)。"""
    def on_event(self, event: BlackboardEvent, offset: int) -> None:
        """黑板事件订阅回调 → 落冷档。"""
        ...

    def record_model_detail(self, intent_id: str, detail: dict) -> None:
        """记模型级细节(prompt/中间推理),黑板不承载,Trace 独有。"""
        ...


# ════════════════════════════════════════════════════════════════════════
# 待实现接口位(NOT-STAGED)—— 第三轮结构审查钉的接口签名。
# 只钉签名 + 标 TODO,实现按 L1/L2 时间线后续实现。这里列明各接口待实现什么。
# ════════════════════════════════════════════════════════════════════════

class TaskCommandPort(Protocol):
    """A1:任务指令(需求方对任务的写操作,除下发外)。TODO。"""
    def cancel(self, task_id: str) -> None: ...
    def pause(self, task_id: str) -> None: ...
    def resume(self, task_id: str) -> None: ...
    def amend(self, task_id: str, patch: dict) -> None: ...
    def clarify(self, task_id: str, answer: dict) -> None:
        """C5:任务生成期澄清(信息缺口→反问→补答),非 loop 运行期。"""
        ...


class AccessGatewayPort(Protocol):
    """A2:机器方接入网关(设备自描述 → AgentCard)。与 MCP tools/list 对齐。TODO。"""
    def on_device_connect(self, descriptor: dict) -> AgentCard:
        """设备连入 → 拉能力清单 → 造 AgentCard → register + spawn。"""
        ...


class EventIngressPort(Protocol):
    """A4:反向入口(设备/环境 → 黑板)。与三个出向 Protocol 对称的一等入向。TODO。

    覆盖三源:设备报障 / Tool 回执线索 / 环境节点(CAMERA)告警。
    """
    def ingest(self, raw: dict) -> list:
        """标准化外部事件 → list[BlackboardEvent] 写黑板。"""
        ...


class MediaStorePort(Protocol):
    """A5:证据媒体存取(MediaRef.uri 的生产/消费)。TODO。"""
    def put(self, data: bytes, media_type: str) -> str:
        """存媒体,返回 uri。"""
        ...

    def get(self, uri: str) -> bytes: ...

    def presign(self, uri: str) -> str:
        """签名直取 URL(前台/设备直接拉)。"""
        ...


class HookPort(Protocol):
    """B3:横切治理链的极薄钩子(借 DeerFlow Middleware 思想,不照抄形状)。
    只 4 个有物理意义的钩子挂在 tool_call 前后;循环检测/上下文压缩留位标 TODO。
    这条链是"安全横切纵 + 自进化横切纵"的代码承载体。TODO。
    """
    def pre_decide(self, ctx: dict) -> dict:
        """决策前:注入 skill 参考 / budget。"""
        ...

    def pre_tool(self, intent: "ActionIntent", ctx: dict) -> "SafetyVerdict":
        """tool_call 前:强制 guardrail(= B2 已在 AgentLoop.act 落地)。"""
        ...

    def post_tool(self, receipt: "ActionReceipt", ctx: dict) -> None:
        """tool_call 后:post_check + observation 回喂 + Trace。"""
        ...

    def on_budget(self, budget, ctx: dict) -> Optional["ActionIntent"]:
        """预算钩子:电量见底 → 强制 return_home(现 Budget 只有数据、无 enforcement)。"""
        ...


class ConnectorPort(Protocol):
    """B5:通信连接器(RAI Connector-Tool 解耦的"通信"那半;Adapter 持有一个 Connector)。
    弱网重连/心跳/topic 订阅的归宿。命名借 RAI,分层是具身通用。TODO。
    """
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def is_alive(self) -> bool: ...
    def send(self, msg: dict) -> None: ...
    def subscribe(self, topic: str, handler) -> None: ...


class TelemetryPumpPort(Protocol):
    """B6:遥测泵 —— 定时轮询各 adapter 的 read_telemetry → 转 TelemetrySample → push。
    解决"谁定时把设备状态泵进遥测通道"。TODO。"""
    def poll_once(self) -> int:
        """轮询一轮所有设备,返回上报条数。"""
        ...


class CheckpointPort(Protocol):
    """B7:断点续跑 —— event-sourcing replay 重建视图/租约/挂起态(不学 DeerFlow 会话快照)。
    前置:board.claim 要走 append(现直接改 dict),租约状态才能从事件重建。TODO。"""
    def replay(self) -> None:
        """从 append-only 事件流重建全部工作态。"""
        ...


class EventStreamPort(Protocol):
    """B8:黑板→前台增量流桥(subscribe 回调 → SSE 队列)。与 SituationView 并列。
    MVP subscribe 是同步回调无背压,真 SSE 需内存队列桥接。TODO。"""
    def open(self, from_offset: int = 0):
        """返回一个可迭代的增量事件流(供 SSE yield)。"""
        ...


class MemoryPort(Protocol):
    """记忆访问接口(hybrid 拓扑 + 四类两层)。只钉"能让 Agent 访问到 memory"的接口,
    实现(私有槽用什么存、事实库怎么积累/检索/巩固)待补。NOT-STAGED。

    四类记忆 × 两层(业界共识,对标 LLMA-Mem/MIRIX/G-Memory):
        工作/情节/语义/程序 × 私有(每Agent替身自己的)/共享(平台)。
    本模块【只管新增的两块】:Agent 私有记忆(私有工作+私有情节)+ 共享语义/事实库;
    黑板(工作-共享)/Trace(情节-共享)/Skill(程序-共享)保持独立、本模块只引用不收编。

    多智能体记忆两大难题(业界 open,我们靠黑板范式解):
        - 私有 vs 共享边界:私有记忆别的 Agent 访问不到,要共享必经黑板(不点对点读别人记忆)。
        - 并发写一致:共享侧走黑板 append-only + 版本 + 租约。
    """
    # ── Agent 私有记忆(每个 Agent 替身一份,含实体设备的云端替身)──
    def store_private(self, agent_id: str, kind: str, item: dict) -> None:
        """写私有记忆。kind: working(工作上下文) / episodic(局部经历/经验)。"""
        ...

    def retrieve_private(self, agent_id: str, kind: str, query: dict = None) -> list:
        """读自己的私有记忆(别的 Agent 访问不到;要共享经黑板)。"""
        ...

    # ── 共享语义/事实库(目标习性/场地知识/误判经验,北向补全 Agent 读它)──
    def read_facts(self, topic: str) -> list:
        """读事实库(如"萨摩耶受惊往灌木钻"、"东南地库对无人机遮挡")。"""
        ...

    def write_fact(self, topic: str, fact: dict, source: str) -> None:
        """写事实(跨任务积累;真上要去重/置信/过期,留实现)。"""
        ...

    # TODO:私有记忆存储(内存→持久化)、事实库积累/检索/巩固(consolidation)、
    #   与黑板/Trace/Skill 的引用关系、per-agent 工作记忆随任务生命周期清理。
    #   对标 LLMA-Mem 的 local/shared/hybrid topology、MIRIX 的 Meta Memory Manager。
