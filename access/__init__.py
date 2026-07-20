"""access —— 【L5 接入层】异构机器人的"Agent 化"注册处 + Tool 网关 + 适配器 + 通信 + 坐标。

这一层最重要也最难:统一接入 + 空地弱网通信。设备接入 = 注册成一个 Agent(有 Agent Card),
不是纯 Tool Provider。上层把它当自主成员协作。

四件事:
    ① Agent 化注册(registry):设备 → AgentCard,能力注册/发现/刷新。
    ② Tool 网关(tool_gateway):动作意图 → 设备指令 → 回执;容纳五类 Tool(G/S/C/V/Y)。
    ③ 协议归一(adapters):MAVLink/ROS2/SDK 三套 → 契约动词;真机/仿真共享 base 接口。
    ④ 通信 + 坐标(telemetry/frames):遥测旁路 + 空地异构坐标统一。

可参考:
    - RAI Connector-Tool 解耦 —— 同一上层逻辑换 Connector 切设备。
    - MCP 自注册 server + tools/list 能力自动发现 + listChanged 热更新 —— 注册范式(协议可换)。
    - Mobius 投递状态机 —— 通信可靠投递。
    - RAI"零改动跨机型"实际仍要换 whoami + Tool 集;适配器宜薄
      (pymavlink/rclpy 直连,不套 MAVROS,少一层封装少一层延迟故障点)。
    - MCP/ROS2/MAVLink 用现成库;骨架阶段全 mock,按真依赖形状设计接口。
"""
