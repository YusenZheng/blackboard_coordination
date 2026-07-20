# STATUS: NOT-STAGED —— 只钉 MemoryPort 接口(见 contracts.interfaces),实现待补
"""memory —— 【记忆模块】hybrid 拓扑 + 四类两层。只管新增的两块,不收编黑板/Trace/Skill。

【为什么有这层】一个 agent 系统的记忆分四类(工作/情节/语义/程序)× 两层(私有/共享)。
之前架构里共享侧有了(黑板=工作·共享、Trace=情节·共享、Skill=程序·共享),
但【每个 Agent 自己的私有记忆】和【共享语义/事实库】漏了 —— 而 memory 不只虚拟 Agent 有,
每个实体设备的云端替身也该有(demo 里"无人机自己归纳出躲藏规律"就是单体 Agent 的私有情节记忆)。

本模块补两块(其余保持独立、只引用):
    ① Agent 私有记忆:每个 Agent 替身自己的 工作记忆(当前推理上下文)+ 情节记忆(局部经历/经验)。
    ② 共享语义/事实库:目标习性/场地知识/误判经验,跨任务积累,北向补全 Agent 读它变准。

【只钉接口,不做实现】MemoryPort 在 contracts.interfaces。怎么存、怎么积累检索巩固,待实现。

可参考:
    - LLMA-Mem —— local/shared/hybrid 三拓扑;本架构走 hybrid(私有情节 + 共享程序/事实)。
    - MIRIX —— Meta Memory Manager + 各类 Memory Manager 的中枢式组织。
    - G-Memory —— 多智能体分层记忆(insight/query/interaction 三层图)。
    - 业界两大 open 难题(私有vs共享边界、并发写一致):本架构靠黑板范式解
      (私有不外露、共享经黑板 append-only+版本+租约),而非"全共享"或"全私有"两极。

"""
