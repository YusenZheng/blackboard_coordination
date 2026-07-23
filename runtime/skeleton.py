# STATUS: STAGED(A类)—— Harness 总装内嵌 coordination-v2 + 保留的 L0 辅助链路
"""runtime.skeleton —— 项目唯一公开运行入口。

默认由 Harness 复用自身 Blackboard/Safety/Access/Assets，执行
coordination-v2 完整闭环。多链路 Harness 演示中的链路1使用同一实现；
链路2–4保留为后续契约级测试。

链路1使用真实 LLM 完成自主 proposal 与群体规划，执行端仍为 MockAdapter；
因此它验证的是 coordination-v2 单进程闭环，不代表真实设备、弱网或分布式
并发已经验证。链路2–4仍是 Harness 的 L0 契约级兼容演示。
"""
from __future__ import annotations

import argparse
import json

from ..access.adapters.mock_adapter import MockAdapter
from ..contracts.agent_card import AgentCard, CapabilitySlot
from ..contracts.blackboard_event import BlackboardEvent, EventType, Ledger
from ..contracts.task import ActionIntent
from ..contracts.types import (CapabilityProfile, Clue, Confidence, ConfidenceLevel,
                               DeviceRef, DeviceState, DeviceType, Position)
from ..contracts.verbs import ActionVerb
from .harness import Harness
from ..contracts.bypass import TelemetrySample
from ..contracts.bypass import EmergencyStop


DEFAULT_INSTRUCTION = "帮我找公园里走失的白色萨摩耶"

def _make_card(dev_id, dtype, battery, tools=("G01",), width_cm=40):
    return AgentCard(
        identity=DeviceRef(device_id=dev_id, device_type=dtype),
        state=DeviceState(battery=battery, endurance_s=600.0),
        capability=CapabilitySlot(
            action_verbs=[ActionVerb.MOVE_TO],
            atomic_tools=list(tools),
            profile=CapabilityProfile(capabilities=["search"], width_cm=width_cm),
        ),
    )


def run_legacy() -> Harness:
    print("=" * 70)
    print("群体 Agent Harness · coordination-v2 + L0 辅助链路")
    print("=" * 70)

    # 两台机器狗共用一个 mock 适配器角色
    dogs = {"dog_a": MockAdapter("dog_a"), "dog_b": MockAdapter("dog_b")}
    h = Harness(adapters=dogs)

    # ── 链路一:Harness 共享层 → coordination-v2 完整协同闭环 ──
    print("\n【链路一 · coordination-v2 happy path】")
    # 只注册 Agent Card，不挂旧 AgentLoop；竞标、判给和执行全部交给 v2。
    h.register_agent(_make_card("dog_a", DeviceType.DOG, battery=1.0))
    h.register_agent(_make_card("dog_b", DeviceType.DOG, battery=0.5))
    result = h.run_coordination_v2(
        "帮我找走失的萨摩耶幼犬",
        event_listener=_print_blackboard_event,
        status_listener=_print_status,
        llm_listener=_print_llm_call,
        session_listener=_print_agent_session,
    )
    task_id = result["task"]["task_id"]
    winner_id = result["task"]["winner"]
    receipt = result["execution"]["receipt"]
    print(
        "  [链路1结果] "
        f"task={task_id} winner={winner_id} "
        f"status={result['task']['status']} "
        f"gateway={result['runtime']['physical_gateway']} "
        f"tool_traces={len(result['execution']['tool_traces'])}"
    )
    h.registry.update_resume(winner_id, bool(receipt and receipt["success"]))

    # 链路2–4仍是兼容测试；仅在 v2 结束后挂载旧 loop，且不回放历史。
    for device_id in ("dog_a", "dog_b"):
        h.attach_legacy_agent_loop(device_id)




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
                source_device=winner_id)
    
    h.blackboard.append(BlackboardEvent(id="clue-1", 
                                        type=EventType.CLUE, 
                                        ledger=Ledger.EVIDENCE,
                                        content={"clue_id": clue.clue_id, 
                                                 "position": clue.position.label,
                                                 "task_id": task_id}, source=winner_id, confidence=0.6))
    print(f"  [线索] {winner_id} 发现线索 clue-1@waypoint_B → 写黑板(事实证据账本)")

    # 事件驱动:各 loop 的 on_event 收到 CLUE → 触发一次重协同(重新评估/改派)
    replanned = h.trigger_replan(task_id, "clue-1")
    print(f"  [重协同] 新线索驱动 → {replanned} 台设备重新评估(黑板状态变→计划跟着变,非一条道跑到黑)")

    # ── 收尾:任务完成 + 遥测/急停/Trace/运营账本 ──
    print("\n【收尾】")
    # B4:设备执行前后经遥测旁路上报状态(旁路 LLM 直更 agent card + 抄送 Trace)
    
    h.telemetry.push(TelemetrySample(device_id=winner_id,
                                     battery=0.8,
                                     position={"label": "waypoint_A"}, 
                                     current_action="搜索中"))
    print(f"  [遥测旁路] {winner_id} 上报电量 1.0→0.8(旁路四账本折叠,直更 agent card + 抄 Trace)")

    # B3:演一次急停(旁路所有软件层直达驱动)
    
    h.estop.signal(EmergencyStop(scope="all", reason="演示:一键急停"))
    # B1 验证:急停生效期间,loop 再发动作 → 被设备侧门控拦下(急停后不照发下一个 intent)
    blocked = h.loops()[winner_id].act(
        ActionIntent(intent_id="after-estop", 
                     device_id=winner_id,
                     verb=ActionVerb.MOVE_TO, 
                     params={"target": "waypoint_C"}, 
                     reversible=True))
    print(f"  [急停门控] 急停中再发动作 → success={blocked.success}({blocked.failure_reason})")
    h.estop.clear("all")

    print(f"  [急停解除] 解除后动作可恢复下发")
    view = h.blackboard.query_view(ledger=Ledger.TASK)
    print(f"  [黑板派生视图] 任务 {task_id} 状态 = {view['tasks'][task_id]['status']}")
    print(f"  [Trace] 协同事件落档 {len(h.trace.all_events())} 条(订阅黑板派生)"
          f" + 模型级细节 {len(h.trace._model_details)} 条(Trace 独有第二数据源,黑板不承载)")
    print(f"  [运营账本] {h.ledger.summary()}")

    print("\n" + "=" * 70)
    print("链路1 coordination-v2 闭环 ✓；链路2–4 L0 兼容检查 ✓")
    print("(执行端仍为 MockAdapter；未验证真机、弱网和分布式并发)")
    print("=" * 70)
    return h


def _print_status(payload: dict) -> None:
    stage = payload.get("stage", "unknown")
    message = payload.get("message", "")
    details = {
        key: value
        for key, value in payload.items()
        if key not in {"stage", "message"}
    }
    suffix = (
        f" {json.dumps(details, ensure_ascii=False, sort_keys=True)}"
        if details
        else ""
    )
    print(f"  [阶段:{stage}] {message}{suffix}")


def _print_llm_call(payload: dict) -> None:
    print(
        "  [LLM] "
        f"operation={payload.get('operation')} "
        f"model={payload.get('model')} "
        f"status={payload.get('status')} "
        f"duration_ms={payload.get('duration_ms')} "
        f"tokens={payload.get('total_tokens')} "
        f"error={payload.get('error_type')}"
    )


def _print_blackboard_event(payload: dict) -> None:
    content = payload.get("content")
    identifiers = {}
    if isinstance(content, dict):
        for key in (
            "task_id",
            "task_revision",
            "coordination_epoch",
            "bid_round",
            "device_id",
            "plan_id",
            "assignment_id",
            "intent_id",
        ):
            if key in content:
                identifiers[key] = content[key]
    print(
        "  [Blackboard] "
        f"v={payload.get('version')} "
        f"type={payload.get('type')} "
        f"source={payload.get('source')} "
        f"ids={json.dumps(identifiers, ensure_ascii=False, sort_keys=True)}"
    )


def _print_agent_session(payload: dict) -> None:
    session = payload.get("session")
    state = session.get("state") if isinstance(session, dict) else None
    current_intent = (
        session.get("current_intent")
        if isinstance(session, dict)
        else None
    )
    print(
        "  [AgentProcessHost] "
        f"device={payload.get('device_id')} "
        f"phase={payload.get('phase')} "
        f"session_exists={payload.get('exists')} "
        f"state={state} "
        f"current_intent={json.dumps(current_intent, ensure_ascii=False)}"
    )


def run(instruction: str = DEFAULT_INSTRUCTION) -> dict:
    """由 Harness 共享六层组件运行 coordination-v2 完整闭环。"""
    print("=" * 78)
    print("SwarmBrain skeleton · Harness + coordination-v2")
    print("拓扑: Coordinator + AgentProcessHost(dog-a, dog-b) + PureAgentLoop × 2")
    print("=" * 78)
    adapters = {
        "dog-a": MockAdapter("dog-a"),
        "dog-b": MockAdapter("dog-b"),
    }
    harness = Harness(adapters=adapters)
    harness.register_agent(
        _make_card("dog-a", DeviceType.DOG, battery=0.90)
    )
    harness.register_agent(
        _make_card("dog-b", DeviceType.DOG, battery=0.65)
    )
    result = harness.run_coordination_v2(
        instruction,
        event_listener=_print_blackboard_event,
        status_listener=_print_status,
        llm_listener=_print_llm_call,
        session_listener=_print_agent_session,
    )
    print("\n【最终结果】")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\nHarness + coordination-v2 skeleton ✓")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the SwarmBrain coordination skeleton"
    )
    parser.add_argument(
        "instruction",
        nargs="?",
        default=DEFAULT_INSTRUCTION,
        help="one-sentence task instruction",
    )
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="run the Harness multi-link demo (chain 1 uses coordination-v2)",
    )
    args = parser.parse_args()
    if args.legacy:
        run_legacy()
    else:
        run(args.instruction)


if __name__ == "__main__":
    main()
