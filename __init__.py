"""swarm_brain —— AI 原生实体群体 Agent Harness。

顶层定性:把单体 Agent Harness 升维成"一群异构实体 Agent 共享黑板协同的运行底座"。
六层(黑板/协同/资产/安全/接入/北向)+ 两条元回路(安全约束纵、自进化成长圈)
+ 两独立顶层(sim 仿真 / eval 评测)。

架构主心骨见:群智大脑_系统架构主心骨_v0.1.md
本骨架 plan 见:.claude/plans/moonlit-growing-stream.md(v2,经对抗性审查修订)

依赖防火墙(下层不许 import 上层):
    contracts ← 谁都 import,它不 import 任何层
    blackboard ← 只 import contracts
    access/safety/assets ← contracts (+ blackboard 读写)
    coordination ← contracts/blackboard/access/safety
    ingress ← contracts/blackboard
    runtime ← 装配所有层
"""

__version__ = "0.1.0"
