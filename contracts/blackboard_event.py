"""contracts.blackboard_event —— 黑板事件 schema + 四类账本 + 事件类型枚举。

【B2 拍板】黑板是协同事件的唯一写入源。Trace 订阅黑板事件流派生落档,不双写。
【地基】append-only:一条事件写进去就不改,新状态用新事件表达。
事件 schema 对齐主心骨:{id, type, content, source, ts, confidence, ttl} + 版本/幂等键。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union


class Ledger(str, Enum):
    """四类账本(读写节奏/一致性要求不同,分离才不互相拖累)。"""
    TASK = "task"            # 任务账本:目标/发起者/紧急度/阶段/责任/失效条件
    EVIDENCE = "evidence"    # 事实证据账本:线索/观测/来源/置信度/是否已验证
    RECEIPT = "receipt"      # 动作回执账本:动作请求/执行者/结果/失败原因/租约占用
    HUMAN = "human"          # 人类确认账本:授权请求/授权人/确认结果(授权点走这里)


class EventType(str, Enum):
    """事件类型枚举(C5/A1:补齐 demo 真实事件类型,不止 skeleton 那 4 个)。"""
    # 任务账本
    TASK_POSTED = "task_posted"          # 任务发布
    BID_ROUND_OPENED = "bid_round_opened"  # v2 竞标窗口开启
    TASK_ASSIGNED = "task_assigned"      # 判给(E04)
    ASSIGNMENT_COMPLETED = "assignment_completed"  # v2 单角色完成
    TASK_DONE = "task_done"              # 完成
    TASK_FAILED = "task_failed"          # 明确失败终态
    TASK_REPLAN = "task_replan"          # 重规划触发
    # 协同(社交动词落到黑板)
    BID = "bid"                          # 应征出价
    CLAIM = "claim"                      # 资源独占申请(租约)
    YIELD = "yield"                      # 主动让位
    HANDOFF = "handoff"                  # 移交跟踪
    ASK_HELP = "ask_help"                # 请求协助
    # 证据
    CLUE = "clue"                        # 线索上报
    FUSION = "fusion"                    # 线索融合结果
    # 人类确认
    AUTH_POINT = "auth_point"            # 授权点请求(不可逆动作 R1 触发)
    AUTH_DECISION = "auth_decision"      # 授权结果
    # 安全 / 回执
    ACTION_INTENT = "action_intent"      # 标准化动作意图
    RECEIPT = "receipt"                  # 动作回执
    SAFETY_INTERCEPT = "safety_intercept"  # 安全拦截
    ESTOP = "estop"                      # 急停(旁路,但事件仍留痕)
    # 资产
    SKILL_REGISTERED = "skill_registered"  # 技能注册(E20)


class AppendStatus(str, Enum):
    """Blackboard 对一次写入请求的权威判定。"""

    APPENDED = "appended"
    DUPLICATE_SAME = "duplicate_same"
    REJECTED = "rejected"
    KEY_CONFLICT = "key_conflict"
    TERMINAL_CONFLICT = "terminal_conflict"


@dataclass(frozen=True)
class AppendResult:
    """结构化写入结果。

    ``offset`` 是 0-based 事件游标，``version`` 是 1-based 全局投影水位。
    duplicate/conflict 返回已有事件自己的位置，而不是当前流尾位置。
    """

    status: AppendStatus
    offset: Optional[int]
    version: Optional[int]
    event_id: str
    existing_event_id: Optional[str] = None
    reason_code: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.status in (AppendStatus.APPENDED, AppendStatus.DUPLICATE_SAME)


@dataclass(frozen=True)
class StoredEvent:
    """带游标的已提交事件；供 ``read_since`` 增量消费。"""

    offset: int
    event: "BlackboardEvent"


@dataclass
class BlackboardEvent:
    """一条黑板事件(append-only,写进去就不改)。

    幂等键 idempotency_key:防 lost update —— 同一逻辑事件重复写只生效一次。
    version:派生视图折叠 / 乐观并发校验用。
    """
    id: str
    # 允许滚动升级期间暂存尚未进入公共枚举的事件值；当前 v2 类型均已补齐。
    type: Union[EventType, str]
    ledger: Ledger
    content: dict                         # 事件负载(可承载调用序列等自由结构)
    source: str                           # 来源(device_id / agent 名 / 模块名)
    ts: float = 0.0                       # 时间戳(epoch 秒)
    confidence: Optional[float] = None    # 可选置信度 0-1
    ttl: Optional[float] = None           # 新鲜度(秒),None=不过期
    version: int = 0                      # 版本(单调递增)
    idempotency_key: Optional[str] = None # 幂等键(去重)
    extra: dict = field(default_factory=dict)
    # W3C Trace Context propagation metadata.  It is transport metadata rather
    # than a business fact, so Blackboard deliberately excludes it from the
    # canonical idempotency fingerprint.
    trace_carrier: dict[str, str] = field(default_factory=dict)
