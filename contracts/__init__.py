"""contracts —— 【地基层】所有层共享的数据契约 + 接口契约。

零依赖:本包不 import swarm_brain 的任何其他层。谁都可以 import 它。

契约演进规矩(A11):
    - 本包被所有层 import,是最贵改的地方。改动走评审,通知各受影响层。
    - 新增字段带默认值 → 向后兼容;删/改字段 → 破坏性变更,需全层 review。
    - CONTRACT_VERSION 每次破坏性变更 +1。
    - 关键 dataclass 预留 extra: dict 开口,便于下游扩展而不改契约本身。
    - 契约改动由架构统一把关。

字段真源(A1/C5):social verb 与 G 系列 Tool 编号以 demo v2.0 附录B 为唯一权威
(demo 是验收锚点),不采用 v5 正文/主心骨的旧版。
"""

# 契约版本号。破坏性变更时 +1,并在 PR 说明受影响层。
CONTRACT_VERSION = 1
