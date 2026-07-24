# STATUS: STAGED(A类,样板)—— Tool 放这里。骨架给 base+registry+1样例(G01),其余照 IO 设计填
"""access.tools —— 具体 Tool 的家。

【回答"Harness 怎么调 Tool":Tool 在这里定义并注册 → tool_gateway 从 ToolRegistry 查到并调用
→ Agent Loop 在循环里发起 tool_call。】

每个 Tool 一个文件(id/所属类/IO schema/mock 实现),启动时注册进 ToolRegistry。
五类(G设备端/S态势/C协同/V社交/Y云端),对齐《寻找小白_ToolSkill_IO设计_v1.2》。

骨架只给:base(ToolSpec 形状)+ registry + 1 个 mock 样例(g01_navigate)。
TODO:照 IO 设计 v1.2 补 G02-G12 / S01-S18 / Y01-Y08 —— 每个照 g01_navigate 的样子写。
"""
