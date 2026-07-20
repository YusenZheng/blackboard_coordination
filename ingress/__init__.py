"""ingress —— 【L6 北向入口】任务生成 pipeline + 事件接入 + 三出向 Protocol。

Harness 人机入口。含"任务生成"处理步骤(用户输入 → 结构化任务包),产物按协同挡位分流。
【北向 UI 挂起】呈现层(态势大屏/授权 UI)本轮不做,但接口层(三出向 Protocol)要钉。

可参考:
    - CAMEL Task Specifier —— 粗需求 → 结构化任务。
    - DeerFlow 多入口(Web/IM/API 进同一 Runtime)—— 多入口思想。
    - 任务理解可做成 pipeline(意图→补全→路由),而非一个大模型一次算完。
"""
