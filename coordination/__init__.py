"""coordination —— 【L2 协同层 · 发动机】Agent Loop + 挡位选择器 + 冲突分治。

两个正交维度:
    维度一·任务分配挡位(mode_selector):自主应征/master-worker/workflow/单机直控。
    维度二·设备配合(不预定义原语,只给中性能力,配合策略靠模型涌现)。
每台设备一个云端虚拟 Agent 跑常驻 Agent Loop。

【机制/策略分离】框架给中性能力(写黑板意图/读黑板痕迹/租约占用/调 Tool),
接力/蹲守/合围/让位全是模型读黑板后自主决策、彼此碰出来的。涌现在策略层。

可参考:
    - CAMEL Workforce(master-worker + barrier)、FLEET 竞价、EMOS Robot Resume、
      DeerFlow Middleware 链(有序管线)。
    - AgentNet:证明去中心可行但没上真机;真机要处理物理排他占用(本架构用租约)。
    - Agent Loop 本骨架自研薄版,未复用大框架。
"""
