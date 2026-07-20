"""access.adapters —— 三套协议适配器(真机/仿真共享 base 接口)。

【C15】adapters/ 下预置三个占位包 drone_/vehicle_/dog_adapter(而非一个 mock),
让"三类设备接入"的边界在目录结构可见——待实现,目录结构上一目了然。
base.py 做厚:契约动词 → 设备指令翻译 + 坐标/时间对齐钩子。
真机没打通时,demo 用 sim adapter(sim/sim_adapter.py)顶上同一角色。
"""
