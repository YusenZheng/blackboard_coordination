# STATUS: STAGED(A类)—— ★可跑的 walking skeleton(样板间)。python -m swarm_brain.runtime.skeleton
"""runtime.skeleton —— 走通最细链路的空壳样板间。

╔═══════════════════════════════════════════════════════════════════════╗
║  这个 skeleton 证明了什么 / 没证明什么(A9,别被"跑通"误导)              ║
╠═══════════════════════════════════════════════════════════════════════╣
║  跑通级别:L0(契约级)                                                    ║
║  证明了:  ① 数据契约在单进程自洽(六层用同一套 contracts 接得起来)         ║
║           ② 依赖防火墙不打架(下层不 import 上层)                          ║
║           ③ 主循环顺序对(任务→黑板→bid→判给→安全→执行→回执→Trace)         ║
║           ④ 两条链路结构成立(happy path + 授权点R1 + claim/yield 并发)     ║
║  没证明:  ✗ 真 LLM 涌现(bid 是写死的,不是模型读黑板自主决策)——见 L1     ║
║           ✗ 并发一致性(内存 dict,无真并发/无 CAS 防 lost update)         ║
║           ✗ 物理执行(mock adapter print,无真机/真坐标/真时延)           ║
║           ✗ 云端弱网(单进程,无云-端通信/断连)                            ║
║  跑通分级:L0 契约 / L1 真LLM小网格(接 sim/grid_10x10) / L2 仿真闭环 / L3 真机 ║
║  ——对外说"跑通"必带级别后缀。                                            ║
╚═══════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

from ..access.adapters.mock_adapter import MockAdapter
from ..contracts.agent_card import AgentCard, CapabilitySlot
from ..contracts.blackboard_event import BlackboardEvent, EventType, Ledger
from ..contracts.task import ActionIntent
from ..contracts.types import (CapabilityProfile, Clue, Confidence, ConfidenceLevel,
                               DeviceRef, DeviceState, DeviceType, Position)
from ..contracts.verbs import ActionVerb
from .harness import Harness
from ..contracts.types import ParticipationDecision
from ..contracts.bypass import TelemetrySample
from ..contracts.bypass import EmergencyStop

def _make_card(dev_id, dtype, battery, tools=("G01",), width_cm=40):
    return AgentCard(
        identity=DeviceRef(device_id=dev_id, device_type=dtype),
        state=DeviceState(battery=battery),
        capability=CapabilitySlot(atomic_tools=list(tools),
                                  profile=CapabilityProfile(capabilities=["ground_search"],
                                                            width_cm=width_cm)),
    )


def run() -> Harness:
    print("=" * 70)
    print("群体 Agent Harness · walking skeleton(L0 契约级跑通)")
    print("=" * 70)

    # 两台机器狗共用一个 mock 适配器角色
    dogs = {"dog_a": MockAdapter("dog_a"), "dog_b": MockAdapter("dog_b")}
    h = Harness(adapters=dogs)

    # ── 链路一:任务→黑板→多Agent bid→判给→安全check(R0)→执行→回执→Trace ──
    print("\n【链路一 · happy path】")
    # 1. 先注册两台设备(生成云端虚拟 Agent loop)
    h.spawn_agent(_make_card("dog_a", DeviceType.DOG, battery=1.0))
    h.spawn_agent(_make_card("dog_b", DeviceType.DOG, battery=0.5))

    # 2. 北向 ingress 三段 pipeline 生成任务包
    task = h.task_gen.generate("帮我找走失的萨摩耶幼犬")
    print(f"  [任务生成] {task.task_id} 目标={task.goal} 初始信任={task.initial_autonomy_level} "
          f"挡位={h.mode_selector.route(task)}")

    # 3. 任务发布到黑板 → 事件驱动各 loop 自主 bid(写死决策)
    h.blackboard.append(BlackboardEvent(id=f"post-{task.task_id}", 
                                        type=EventType.TASK_POSTED, 
                                        ledger=Ledger.TASK,
                                        content={"task_id": task.task_id, 
                                                 "task_type": task.task_type,
                                                 "goal": task.goal  }, 
                                        source="ingress"))

    # 4. 协同层收集 bid,招投标判给出价最高者
    bids = h.blackboard.read_events(types=[EventType.BID])

    decisions = [ParticipationDecision(device_id=e.content["device_id"],
                                       decision=e.content.get("decision", "reject"),
                                       bid_score=e.content["bid_score"],
                                       reason_codes=[e.content.get("reason", "")]) for e in bids]
    for d in decisions:
        print(f"  [应征] {d.device_id} decision={d.decision} bid={d.bid_score} "
              f"({'；'.join(d.reason_codes)})")
        
    winner = h.conflict.rank_bids(task.task_id, decisions)
    print(f"  [判给] 中标 = {winner.device_id}(bid={winner.bid_score})")

    h.blackboard.append(BlackboardEvent(id=f"assign-{task.task_id}", 
                                        type=EventType.TASK_ASSIGNED, 
                                        ledger=Ledger.TASK,
                                        content={"task_id": task.task_id, 
                                                 "device_id": winner.device_id}, 
                                        source="coord"))

    # 5. 中标者产出动作意图 → 安全 check(R0 放行)→【中标者的 loop 自己发起 tool_call】→ 回执回黑板
    intent = ActionIntent(intent_id="mv-1", 
                          device_id=winner.device_id,
                          verb=ActionVerb.MOVE_TO, 
                          params={"target": "waypoint_A"}, 
                          reversible=True)
    
    verdict = h.guardrail.check(intent)
    print(f"  [安全] {intent.intent_id} {verdict.reversibility} → "
          f"{'放行' if verdict.allowed else '拦截/需授权'}({verdict.reason})")
    
    # loop 自己发起 tool_call(act 内部经 tool_gateway 执行 + observation 回执写黑板),非 harness 代劳
    receipt = h.loops()[winner.device_id].act(intent)
    print(f"  [tool_call] {winner.device_id} 的 loop 自己调工具执行,回执 success={receipt.success}")












    # 另演一次 call_tool:经 ToolRegistry 查到 G01 并 run(证明"注册→查表→run"通)
    g01_result = h.tool_gateway.call_tool("G01", {"destination": "waypoint_A", "movement_mode": "四足行走"})
    print(f"  [call_tool] 查注册表调 G01 → {g01_result.get('status')}")

    h.registry.update_resume(winner.device_id, receipt.success)   # C14 履历闭环




    # ── 链路二:安全(不可逆R1授权点)+ 并发(claim/yield) ──
    print("\n【链路二 · 授权点 R1 + 并发 claim/yield】")

    # 5b. 不可逆动作:钻封闭缝隙 → R1 → 授权点挂起 → mock 确认 → 放行
    intent2 = ActionIntent(intent_id="enter-1", 
                           device_id="dog_a",
                           verb=ActionVerb.MOVE_TO,
                           params={"target": "绿篱后缝隙", "enclosed": True}, 
                           reversible=False)
    v2 = h.guardrail.check(intent2)
    print(f"  [安全] {intent2.intent_id} {v2.reversibility} → needs_auth={v2.needs_auth}")

    if v2.needs_auth:
        h.auth_gate.suspend(intent2.intent_id, {"desc": "机器狗甲钻绿篱后封闭缝隙"})
        h.auth_gate.auto_confirm(intent2.intent_id, approver="指挥官")   # mock 5s 无否决放行
        r2 = h.tool_gateway.dispatch(intent2)
        print(f"  [执行] 授权后放行,回执 success={r2.success}")
 
    # 6b. 并发资源冲突:两狗 claim 同一窄巷 → 一方 yield → 仲裁优先级
    print("  --- 并发 claim/yield ---")
    ok_a = h.conflict.claim("passage_X", "dog_a")
    ok_b = h.conflict.claim("passage_X", "dog_b")   # 重叠 claim 被拒
    print(f"  [claim] dog_a 占 passage_X={ok_a}  dog_b 占 passage_X={ok_b}(重叠被拒)")
    h.blackboard.append(BlackboardEvent(id="claim-a", 
                                        type=EventType.CLAIM, 
                                        ledger=Ledger.RECEIPT,
                                        content={"resource": "passage_X", "device_id": "dog_a"}, 
                                        source="dog_a"))
    
    # dog_b 被拒后可 yield 改做别的 —— 具体"改去做什么"的配合策略由算法组在 L1 涌现,骨架不预设
    print("  [yield] dog_b 让出 passage_X,改派其他资源(配合策略由算法组 L1 决策,非骨架编排)")
    # 僵局仲裁演示:三层优先级
    winner_res = h.conflict.arbitrate("passage_X", [
        {"device_id": "dog_a", "safety_rank": 0, "capability_rank": 2, "request_ts": 1.0},
        {"device_id": "dog_b", "safety_rank": 0, "capability_rank": 1, "request_ts": 2.0},
    ])
    print(f"  [仲裁] 三层优先级(P1安全>P2能力>P3时间)→ {winner_res}")

    # ── 链路三:可插拔挡位 —— 同一个任务,换集中式挡走另一条路 ──
    print("\n【链路三 · 可插拔挡位:集中式(master-worker)对照】")
    task_c = h.task_gen.generate("再找一只走失的狗")
    task_c.extra["mode"] = "master_worker"        # 这次指定集中式挡
    mode = h.mode_selector.route(task_c)
    print(f"  [选择器] 任务 {task_c.task_id} 挡位 = {mode}(上一个任务是 autonomous,同框架不同挡)")
    if mode == "master_worker":
        master = h.make_master()
        master.assign(task_c)                     # master 点名指派,不经竞价 bid
    print("  → 证明:同一 Harness,分配挡位可插拔(自主应征=竞价涌现 / 集中式=master点名),两条路")

    # ── 链路四:共享黑板自组织 —— 新线索进黑板 → 事件驱动重协同(B6)──
    print("\n【链路四 · 新线索 → 就地重协同(共享黑板自组织)】")

    # 执行中冒出一条 mock 线索(真上是设备 Tool 回执/环境事件经 event_ingress 进来)
    clue = Clue(clue_id="clue-1", 
                position=Position(label="waypoint_B", area="片区B"),
                confidence=Confidence(level=ConfidenceLevel.MID, score=0.6, method="mock"),
                source_device=winner.device_id)
    
    h.blackboard.append(BlackboardEvent(id="clue-1", 
                                        type=EventType.CLUE, 
                                        ledger=Ledger.EVIDENCE,
                                        content={"clue_id": clue.clue_id, 
                                                 "position": clue.position.label,
                                                 "task_id": task.task_id}, source=winner.device_id, confidence=0.6))
    print(f"  [线索] {winner.device_id} 发现线索 clue-1@waypoint_B → 写黑板(事实证据账本)")

    # 事件驱动:各 loop 的 on_event 收到 CLUE → 触发一次重协同(重新评估/改派)
    replanned = h.trigger_replan(task.task_id, "clue-1")
    print(f"  [重协同] 新线索驱动 → {replanned} 台设备重新评估(黑板状态变→计划跟着变,非一条道跑到黑)")

    # ── 收尾:任务完成 + 遥测/急停/Trace/运营账本 ──
    print("\n【收尾】")
    # B4:设备执行前后经遥测旁路上报状态(旁路 LLM 直更 agent card + 抄送 Trace)
    
    h.telemetry.push(TelemetrySample(device_id=winner.device_id, 
                                     battery=0.8,
                                     position={"label": "waypoint_A"}, 
                                     current_action="搜索中"))
    print(f"  [遥测旁路] {winner.device_id} 上报电量 1.0→0.8(旁路四账本折叠,直更 agent card + 抄 Trace)")

    # B3:演一次急停(旁路所有软件层直达驱动)
    
    h.estop.signal(EmergencyStop(scope="all", reason="演示:一键急停"))
    # B1 验证:急停生效期间,loop 再发动作 → 被设备侧门控拦下(急停后不照发下一个 intent)
    blocked = h.loops()[winner.device_id].act(
        ActionIntent(intent_id="after-estop", 
                     device_id=winner.device_id,
                     verb=ActionVerb.MOVE_TO, 
                     params={"target": "waypoint_C"}, 
                     reversible=True))
    print(f"  [急停门控] 急停中再发动作 → success={blocked.success}({blocked.failure_reason})")
    h.estop.clear("all")

    print(f"  [急停解除] 解除后动作可恢复下发")
    h.blackboard.append(BlackboardEvent(id=f"done-{task.task_id}", 
                                        type=EventType.TASK_DONE, 
                                        ledger=Ledger.TASK,
                                        content={"task_id": task.task_id}, 
                                        source="coord"))
    view = h.blackboard.query_view(ledger=Ledger.TASK)
    print(f"  [黑板派生视图] 任务 {task.task_id} 状态 = {view['tasks'][task.task_id]['status']}")
    print(f"  [Trace] 协同事件落档 {len(h.trace.all_events())} 条(订阅黑板派生)"
          f" + 模型级细节 {len(h.trace._model_details)} 条(Trace 独有第二数据源,黑板不承载)")
    print(f"  [运营账本] {h.ledger.summary()}")

    print("\n" + "=" * 70)
    print("L0 契约级跑通 ✓ —— 契约自洽/依赖不打架/主循环顺序对/两链路结构成立")
    print("(未证明:真LLM涌现/并发一致/物理执行/弱网 —— 见 skeleton 顶部说明)")
    print("=" * 70)
    return h


if __name__ == "__main__":
    run()
