"""contracts.verbs —— 统一动作契约的动词表。

【真源裁决 A1/C5】social verb 以 demo v2.0 附录B(V01-V05)为唯一权威,不用 v5 正文的旧版。
理由:demo 是验收锚点,claim/yield 是"抢道让位钳形合围"两大涌现点的核心动词
——阶段二"两狗对窄巷 claim、乙主动 yield",L2 涌现 Skill 触发条件就是"两设备 claim
同一资源 + 一方 yield"。旧版(bid/reject/ask_help/handoff/report)缺 claim/yield,接不上黑板。

契约边界:契约以上是我们自研的 agentic 群智大脑;契约以下每台机器怎么定位/导航/行走
是各设备自己的事。Agent 永远只调契约动词,碰不到底层控制。

【机制/策略分离】动词在这里是"机制"(定义设备会 bid/claim);"何时 bid、bid 给谁、
要不要 yield"是协同层 Agent Loop 读黑板后自主决策的"策略"。
"""
from __future__ import annotations

from enum import Enum


class ActionVerb(str, Enum):
    """动作动词 —— 设备的物理动作(对齐 v5 §5.1,与附录 G 系列 Tool 对应)。"""
    MOVE_TO = "move_to"          # 定点导航(内含避障)
    FOLLOW = "follow"            # 跟随
    HOVER = "hover"              # 悬停
    PICK_PLACE = "pick_place"    # 抓取/放置
    RETURN_HOME = "return_home"  # 返航
    STOP = "stop"                # 停止
    FORMATION = "formation"      # 编队


class SocialVerb(str, Enum):
    """社交动词 —— 设备在共享黑板上表达协同意图(对齐 demo 附录B V01-V05)。

    这是我们对标准 tool 接口的关键扩展:数字域框架的工具只有"做事"类动词,
    我们加一组"共事"类动词,让设备能在黑板上主动应征/占用/让位/移交/求助
    ——分布式多 Agent 自组织的语言基础,市面机器人接口层没有这一类。
    """
    BID = "bid"            # V01 应征出价(reject 不再是独立动词,降级为 bid 结果的 outcome)
    CLAIM = "claim"        # V02 申请资源独占(如窄巷通行权)—— 租约
    YIELD = "yield"        # V03 主动让位(打包移交)—— demo 涌现核心
    HANDOFF = "handoff"    # V04 移交正在进行的跟踪任务(附最后坐标+时间戳)
    ASK_HELP = "ask_help"  # V05 请求协助


# 注:
#   - reject(拒绝):不是社交动词,是 BID 事件的 outcome 字段(ParticipationDecision.accept=False)。
#     拒绝仍写黑板(带理由,大屏可见),但归为 bid 的一种结果,不是独立动词。
#   - report(上报状态):不是社交动词,走 Telemetry 旁路(contracts.bypass.TelemetryChannel),
#     对应 G 系列状态上报,高频、不进 Agent context。
