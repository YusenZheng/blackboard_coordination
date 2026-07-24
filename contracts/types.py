"""contracts.types —— 附录 A.1 通用数据类型。

跨 Tool/Skill/层复用,保证数据在节点间可透传、可校验。字段对齐补充说明附录 A.1。

关键设计(A12 面向真实的 mock):
    - Position 第一天就带 frame_id + stamp —— 这是最底层类型,定错返工代价最大,
      直接对应主心骨点名的"坐标系硬骨头"(无人机 3D 空域 GPS vs 地面 2D 里程计)。
      MVP 阶段 frame_id 可填默认 "world",但字段必须在,不能等真接 ROS2/MAVLink 才加。
    - CapabilityProfile 带 width_cm/lowest_posture_cm 等明确尺寸字段(按剧本 IO 设计)——
      demo 有"机器狗宽40cm仅进>40cm间隙"、G03"不穿透玻璃/织物/浓密树冠"这类物理约束。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ── 基础 ──────────────────────────────────────────────────────────────

class DeviceType(str, Enum):
    DRONE = "drone"          # 无人机
    VEHICLE = "vehicle"      # 无人车
    DOG = "dog"              # 机器狗
    CAMERA = "camera"        # 固定摄像头(环境节点)
    OTHER = "other"


@dataclass
class Position:
    """可展示、可关联的位置。

    【按剧本 IO 设计 v1.2 校准】剧本大多只给"J5窄巷"这种标签、无绝对坐标,
    故 label 必填、坐标可空(原剧本未给坐标不虚构)。
    A12:coordinate_system + stamp 从第一天就在——空地异构下无人机走 GPS/RTK、
    地面走里程计/SLAM,靠 coordinate_system 标坐标系、靠 access.frames 做统一世界坐标对齐。
    """
    label: str                       # 剧本坐标/区域标记/路线点,如 J5、M2、东南片区(必填)
    area: str = ""                   # 所属公园子区域,如 灌木区/窄巷区/台阶区
    coordinate_system: str = ""      # 坐标系标识;只有地图服务给坐标时填(= 旧 frame_id)
    x_m: Optional[float] = None      # 地图横坐标(米),坐标可空
    y_m: Optional[float] = None
    z_m: Optional[float] = None
    accuracy_m: Optional[float] = None   # 位置误差半径(米)
    stamp: float = 0.0               # 采样时间戳(epoch 秒);时间同步的锚
    description: str = ""            # 自然语言补充
    extra: dict = field(default_factory=dict)


@dataclass
class MediaRef:
    """照片/视频/传感器证据的引用(原始数据存对象存储,这里只放引用)。"""
    uri: str
    media_type: str = "image"        # image / video / pointcloud / thermal
    stamp: float = 0.0
    extra: dict = field(default_factory=dict)


class ConfidenceLevel(str, Enum):
    """五级描述,对齐剧本 IO 设计(中文五级)。"""
    LOW = "低"
    MID_LOW = "中低"
    MID = "中"
    MID_HIGH = "中高"
    HIGH = "高"


@dataclass
class Confidence:
    """置信度。【按剧本校准】同时支持五级描述 + 数值(仅剧本/融合给数值时 score 才填)。

    剧本"92%"→ score=0.92, level=高;没明确数值时 score 留空。
    """
    level: ConfidenceLevel
    reason: str = ""                 # 置信度依据
    score: Optional[float] = None    # 0.0-1.0,仅剧本或融合算法给数值时填
    method: str = ""                 # 来源:sensor/rule/fusion/human_report/trace_validation
    extra: dict = field(default_factory=dict)


# ── 设备身份 / 状态 / 能力(Agent Card 三件套的底层类型) ─────────────────

@dataclass
class DeviceRef:
    """设备身份。"""
    device_id: str
    device_type: DeviceType
    vendor: str = ""                 # 厂商
    owner: str = ""                  # 所属客户/单位
    extra: dict = field(default_factory=dict)


@dataclass
class DeviceState:
    """可计算的实时状态(Telemetry 高频更新的就是它)。"""
    battery: float = 1.0             # 电量 0-1
    endurance_s: float = 0.0         # 预估剩余续航(秒)
    position: Optional[Position] = None
    current_action: str = "idle"     # 当前动作
    online: bool = True
    healthy: bool = True
    busy: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class CapabilityProfile:
    """能力与物理约束。上层 bid 硬过滤读它(与 TaskRequirement 比对)。

    【按剧本 IO 设计校准】用 capabilities/constraints 列表 + 明确物理尺寸字段
    (width_cm/lowest_posture_cm/step_limit_cm/payload_kg)——这是 demo 物理约束核心:
    机器狗 width_cm=40 只进 >40cm 缝隙、无人车 width_cm=55 进不去 45cm 绿篱(物理唯一解)。
    """
    capabilities: list = field(default_factory=list)   # 能力列表,如 ["aerial_search","thermal"]
    constraints: list = field(default_factory=list)    # 限制,如 ["设备不防水","热成像不穿透玻璃"]
    width_cm: Optional[float] = None                   # 设备宽度(cm)—— 物理唯一解关键
    lowest_posture_cm: Optional[float] = None          # 最低姿态高度(cm)
    step_limit_cm: Optional[float] = None              # 可通过台阶高度(cm)
    payload_kg: Optional[float] = None                 # 载重(kg)
    onboard_skills: list = field(default_factory=list) # 能调用的本地 Skill 名
    extra: dict = field(default_factory=dict)


# ── bid 链路 5 类型(A2:skeleton 跑通就要用,不能用裸 dict 糊) ──────────

@dataclass
class TaskRequirement:
    """任务对能力的需求(bid 两段式硬过滤拿它比 CapabilityProfile)。

    TODO(接缝·命名词表待锁定):required_capabilities 里放什么标识?——能力名(如 aerial_search)
        还是 Tool id(如 G01)?当前 task_gen 生成 Tool id、registry._matches 拿去比 atomic_tools,
        但 CapabilityProfile.capabilities 用能力名。Card 能力 / 本字段 / ActionVerb / tool_id 现为
        多套命名,须由总监拍定统一一套(改契约走审批),再据此改本注释与各生产/消费点。
    """
    required_capabilities: list = field(default_factory=list)  # 命名待锁定(见上 TODO)
    min_battery: float = 0.0
    space_constraints: list = field(default_factory=list)      # list[SpaceConstraint]
    extra: dict = field(default_factory=dict)


@dataclass
class DeviceCandidate:
    """在线候选设备(Y06 能力查询返回,不代替设备自己接单)。"""
    device: DeviceRef
    state: DeviceState
    capability: CapabilityProfile
    distance_to_task: float = 0.0
    extra: dict = field(default_factory=dict)


@dataclass
class ParticipationDecision:
    """设备自主接单响应(E03"设备自主接单评估"的结构化产物)。

    【按剧本校准】decision 三值 accept/reject/conditional(剧本有附条件接单);
    reason_codes 列表(接单/拒绝/附条件都带原因)。bid_score 保留(架构 claim/yield 竞价用)。
    """
    device_id: str
    decision: str = "reject"         # accept / reject / conditional
    proposed_role: str = ""          # 设备主动提出的角色
    reason_codes: list = field(default_factory=list)   # 原因码
    bid_score: float = 0.0           # 出价(架构竞价机制用;剧本层可不用)
    estimated_endurance_margin_s: Optional[int] = None # 续航裕量(秒)
    extra: dict = field(default_factory=dict)

    @property
    def accept(self) -> bool:
        """便捷:是否接单(accept 或 conditional 都算参与)。"""
        return self.decision in ("accept", "conditional")


@dataclass
class Assignment:
    """设备到任务/区域的分工(判给,对应 E04)。"""
    task_id: str
    device_id: str
    area_label: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class SpaceConstraint:
    """空间约束(对应 E18 物理唯一性验证:如"缺口 45cm,仅机器狗可入")。"""
    label: str                       # 如 "M2绿篱缺口"
    min_gap_cm: Optional[float] = None
    passable_by: list = field(default_factory=list)  # 能通过的 device_type 列表
    extra: dict = field(default_factory=dict)


# ── 线索 / 证据(demo 找狗链路用;各 Tool 输出先留占位) ──────────────────

@dataclass
class Clue:
    """完整线索对象(G05 线索上报 / Y03 线索融合的基本单元)。"""
    clue_id: str
    position: Position
    confidence: Confidence
    source_device: str = ""
    stamp: float = 0.0
    media: list = field(default_factory=list)   # list[MediaRef]
    verified: bool = False
    extra: dict = field(default_factory=dict)


# TODO:各 Tool 的稳定命名输出类型 —— FusionResult / MaterialFusionResult /
#   WeatherResult / BatteryCheckResult / HeatFinding / HumanReport / InferenceRef …
#   附录 A.1 全表。skeleton 用不到,先留占位,认领 Tool 的人补齐。

# TODO:SkillCandidate / ValidationResult / RegistrationItem —— 见 assets.skill,
#   与 Skill 七元组一起定义,避免循环依赖(这里只放通用基础类型)。
