"""safety —— 【L4 安全平面 · 横切一纵】三层贯通 + 信任 A0-A4 + 可逆性 R0/R1/R2 + 急停。

不是一层,是横插主循环每个 tool_call 前后的 Guardrail/Hook 管线。动作放行须同时过三层:
语义合规 / 群体安全(碰撞·激化·不一致·死锁) / 物理门控(可逆性)。

【两套分级分清,别混用】
    - 信任自治等级 A0-A4(autonomy.py):人参与多深,任务路由时的 policy。
    - 动作可逆性 R0/R1/R2(reversibility.py):某个动作能不能自主做,pre-tool hook。
    - 合成规则:可逆性门控优先于信任等级——无论 A3/A4,不可逆动作(R1)仍强制走授权点。
【急停】旁路所有软件层直达设备驱动(contracts.bypass.EmergencyStopBus),软件可崩急停不能崩。

可参考:
    - DeerFlow SkillScan(离线确定性硬阻断)—— 动作/技能下发前的确定性校验。
    - OM1 固件级急停 + 安全回路时序隔离 —— 急停不经 LLM、独立回路。
    - RAI Constitution 是纯 prompt 软约束、无运行时强制拦截;本架构群体安全和物理门控
      走运行时硬拦截,不只依赖 prompt。
"""
