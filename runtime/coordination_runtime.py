"""Formal coordination-v2 runtime assembly.

This module wires the existing ingress, Blackboard, coordination, safety and
access ports into one complete task run. ``runtime.skeleton`` is the public
entrypoint and delegates its default execution here.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

from ..blackboard.board import Blackboard
from ..contracts.blackboard_event import Ledger
from ..coordination.action_executor import ActionExecutor
from ..coordination.adapters import (
    MockPhysicalActionGateway,
    NullSkillReferenceProvider,
    StaticSafetyPort,
)
from ..coordination.agent_loop import PureAgentLoop
from ..coordination.agent_process import AgentProcessHost
from ..coordination.blackboard_client import BlackboardClient
from ..coordination.coordinator import Coordinator
from ..coordination.file_session_store import (
    FileCoordinatorStore,
    FileTaskSessionStore,
)
from ..coordination.models import (
    AgentProcessSpec,
    CandidateAssignmentPlan,
    CoordinationEventType,
    event_type_value,
    make_blackboard_event,
    to_json_value,
)
from ..coordination.ports import (
    GroupPlanningPolicyPort,
    LocalProposalPolicyPort,
)
from ..ingress.task_gen import (
    IntentInterpreterPort,
    TaskGen,
    task_package_to_v2_content,
)
from ..llm import LLMConfig
from .deepseek import (
    DeepSeekClient,
    DeepSeekGroupPlanningPolicy,
    DeepSeekIntentInterpreter,
    DeepSeekLocalProposalPolicy,
)


@dataclass(frozen=True)
class DeviceRuntimeConfig:
    device_id: str
    battery: float
    success_rate: float
    endurance_s: float = 600.0
    capabilities: tuple[str, ...] = ("search",)
    action_verbs: tuple[str, ...] = ("move_to",)

    def public_snapshot(self, updated_at: float) -> dict:
        return {
            "device_id": self.device_id,
            "device_type": "dog",
            "online": True,
            "healthy": True,
            "busy": False,
            "busy_task_id": None,
            "battery": self.battery,
            "endurance_s": self.endurance_s,
            "position": None,
            "current_action": "idle",
            "capabilities": list(self.capabilities),
            "action_verbs": list(self.action_verbs),
            "constraints": [],
            "width_cm": 40,
            "lowest_posture_cm": None,
            "step_limit_cm": None,
            "payload_kg": None,
            "success_rate": self.success_rate,
            "card_version": 1,
            "state_updated_at": updated_at,
        }


class RuntimeClock:
    """Controllable event clock used to close a bid window without wall-time sleep."""

    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("clock cannot move backwards")
        self.value += seconds


class ObservedGroupPlanningPolicy:
    """Record safe policy diagnostics while preserving Coordinator fallback."""

    def __init__(self, inner: GroupPlanningPolicyPort) -> None:
        self.inner = inner
        self.candidate: Optional[CandidateAssignmentPlan] = None
        self.error: Optional[str] = None

    def plan(self, planning_input, timeout_s: float) -> CandidateAssignmentPlan:
        try:
            self.candidate = self.inner.plan(planning_input, timeout_s)
            return self.candidate
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            raise


LocalPolicyFactory = Callable[[str], Optional[LocalProposalPolicyPort]]
RuntimeListener = Callable[[dict], None]


class CoordinationRuntime:
    """Execute one coordination-v2 task from instruction through TASK_DONE."""

    def __init__(
        self,
        *,
        llm_config: Optional[LLMConfig] = None,
        intent_interpreter: Optional[IntentInterpreterPort] = None,
        local_policy_factory: Optional[LocalPolicyFactory] = None,
        group_policy: Optional[GroupPlanningPolicyPort] = None,
        devices: Optional[list[DeviceRuntimeConfig]] = None,
        bid_window_s: float = 30.0,
    ) -> None:
        if bid_window_s <= 0:
            raise ValueError("bid_window_s must be positive")
        self.llm_config = llm_config or LLMConfig.from_env()
        self._llm_listener: Optional[RuntimeListener] = None
        self.intent_interpreter = intent_interpreter or DeepSeekIntentInterpreter(
            DeepSeekClient(self.llm_config)
        )
        self.local_policy_factory = local_policy_factory or (
            lambda _device_id: DeepSeekLocalProposalPolicy(
                DeepSeekClient(
                    self.llm_config,
                    telemetry_listener=self._llm_listener,
                )
            )
        )
        self.group_policy = group_policy or DeepSeekGroupPlanningPolicy(
            DeepSeekClient(self.llm_config)
        )
        self.devices = devices or [
            DeviceRuntimeConfig("dog-a", battery=0.90, success_rate=0.95),
            DeviceRuntimeConfig("dog-b", battery=0.65, success_rate=0.90),
        ]
        if len(self.devices) < 2:
            raise ValueError("coordination demo requires at least two devices")
        if len({item.device_id for item in self.devices}) != len(self.devices):
            raise ValueError("device_id values must be unique")
        self.bid_window_s = bid_window_s
        self.task_gen = TaskGen(self.intent_interpreter)

    def run(
        self,
        instruction: str,
        *,
        event_listener: Optional[RuntimeListener] = None,
        status_listener: Optional[RuntimeListener] = None,
        llm_listener: Optional[RuntimeListener] = None,
        session_listener: Optional[RuntimeListener] = None,
    ) -> dict:
        if not instruction.strip():
            raise ValueError("instruction is required")
        self._llm_listener = llm_listener
        self._attach_llm_listener(self.intent_interpreter, llm_listener)
        self._attach_llm_listener(self.group_policy, llm_listener)

        _notify(
            status_listener,
            {
                "stage": "runtime_starting",
                "message": "初始化 Blackboard 与设备运行时",
            },
        )
        clock = RuntimeClock()
        board = Blackboard(clock=clock)
        client = BlackboardClient(board)
        if event_listener is not None:
            board.subscribe(
                lambda event, offset: _notify(
                    event_listener,
                    {
                        "offset": offset,
                        "version": event.version,
                        "type": event_type_value(event),
                        "source": event.source,
                        "event_id": event.id,
                        "ts": event.ts,
                        "content": to_json_value(event.content),
                    },
                )
            )
        for device in self.devices:
            board.upsert_agent_snapshot(device.public_snapshot(clock.value))

        _notify(
            status_listener,
            {
                "stage": "intent_interpreting",
                "message": "LLM 正在把用户指令转换为结构化任务",
            },
        )
        task = self.task_gen.generate(instruction)
        task_payload = task_package_to_v2_content(task)
        post_result = client.append(
            make_blackboard_event(
                CoordinationEventType.TASK_POSTED,
                Ledger.TASK,
                task_payload,
                "ingress",
                (
                    f"task-posted:{task_payload['task_id']}:"
                    f"{task_payload['task_revision']}"
                ),
            )
        )
        if not post_result.accepted:
            raise RuntimeError(
                f"TASK_POSTED rejected: {post_result.reason_code or post_result.status.value}"
            )
        _notify(
            status_listener,
            {
                "stage": "task_posted",
                "message": "任务已进入权威黑板",
                "task_id": task_payload["task_id"],
            },
        )

        observed_group = ObservedGroupPlanningPolicy(self.group_policy)
        with tempfile.TemporaryDirectory(
            prefix="swarmbrain-coordination-"
        ) as work_root:
            coordinator = Coordinator(
                blackboard=client,
                store=FileCoordinatorStore(work_root),
                group_policy=observed_group,
                bid_window_s=self.bid_window_s,
                group_policy_timeout_s=self.llm_config.timeout_seconds,
            )
            hosts = self._build_hosts(client, work_root)

            _notify(
                status_listener,
                {
                    "stage": "bid_round_opening",
                    "message": "Coordinator 正在打开竞标轮次",
                },
            )
            task_events_processed = coordinator.poll_once(timeout_s=0)
            _notify(
                status_listener,
                {
                    "stage": "agents_bidding",
                    "message": "dog-a 与 dog-b 正在独立分析并报价",
                },
            )
            bid_processed = {
                device_id: host.poll_once(timeout_s=0)
                for device_id, host in hosts.items()
            }
            local_model_errors = {
                device_id: host.last_local_proposal_error
                for device_id, host in hosts.items()
            }
            self._publish_sessions(
                session_listener,
                hosts,
                task_payload["task_id"],
                "after_bid",
            )
            bid_events_processed = coordinator.poll_once(timeout_s=0)

            _notify(
                status_listener,
                {
                    "stage": "group_planning",
                    "message": "群体模型正在集中分析有效 BID",
                },
            )
            clock.advance(self.bid_window_s + 1.0)
            close_outputs = coordinator.tick()
            if [event_type_value(item) for item in close_outputs] != [
                CoordinationEventType.TASK_ASSIGNED.value
            ]:
                raise RuntimeError("bid round did not converge to TASK_ASSIGNED")

            assigned_view = self._task_view(client, task_payload)
            current_plan = assigned_view.get("current_plan")
            if not isinstance(current_plan, dict):
                raise RuntimeError("TASK_ASSIGNED view has no committed plan")
            assignments = current_plan.get("assignments", [])
            if len(assignments) != 1:
                raise RuntimeError("demo expects exactly one role assignment")
            winner = str(assignments[0]["device_id"])
            if winner not in hosts:
                raise RuntimeError("committed winner has no AgentProcessHost")
            _notify(
                status_listener,
                {
                    "stage": "task_assigned",
                    "message": f"任务已判给 {winner}",
                    "winner": winner,
                    "plan_id": current_plan["plan_id"],
                },
            )

            assignment_processed: dict[str, int] = {}
            for device_id in sorted(hosts):
                if device_id != winner:
                    assignment_processed[device_id] = hosts[device_id].poll_once(
                        timeout_s=0
                    )
            _notify(
                status_listener,
                {
                    "stage": "executing",
                    "message": f"{winner} 正在执行受限动作并等待回执",
                    "winner": winner,
                },
            )
            assignment_processed[winner] = hosts[winner].poll_once(timeout_s=0)
            dispatched_by_device = {
                device_id: list(host.action_gateway.dispatched_intent_ids)
                for device_id, host in hosts.items()
            }
            self._publish_sessions(
                session_listener,
                hosts,
                task_payload["task_id"],
                "after_assignment_execution",
            )

            _notify(
                status_listener,
                {
                    "stage": "completing",
                    "message": "Coordinator 正在校验回执并收敛终态",
                },
            )
            terminal_events_processed = coordinator.poll_once(timeout_s=0)
            self._publish_sessions(
                session_listener,
                hosts,
                task_payload["task_id"],
                "before_terminal_cleanup",
            )
            terminal_processed = {
                device_id: host.poll_once(timeout_s=0)
                for device_id, host in hosts.items()
            }
            sessions_cleared = {
                device_id: host.store.load_session(task_payload["task_id"]) is None
                for device_id, host in hosts.items()
            }
            self._publish_sessions(
                session_listener,
                hosts,
                task_payload["task_id"],
                "after_task_done_cleanup",
            )

        watermark = client.high_watermark()
        task_view = self._task_view(client, task_payload)
        bid_view = client.query_view(
            Ledger.TASK.value,
            {
                "view_type": "bid_round",
                "task_id": task_payload["task_id"],
                "task_revision": task_payload["task_revision"],
                "coordination_epoch": current_plan["coordination_epoch"],
                "bid_round": 1,
            },
            watermark,
            0,
        )
        action_view = client.query_view(
            Ledger.RECEIPT.value,
            {
                "view_type": "action",
                "task_id": task_payload["task_id"],
                "task_revision": task_payload["task_revision"],
            },
            watermark,
            0,
        )
        terminal_view = client.query_view(
            Ledger.TASK.value,
            {
                "view_type": "terminal",
                "task_id": task_payload["task_id"],
                "task_revision": task_payload["task_revision"],
            },
            watermark,
            0,
        )
        event_sequence = [
            {
                "version": envelope.event.version,
                "type": event_type_value(envelope.event),
                "source": envelope.event.source,
            }
            for envelope in client.read_since(-1, None, 100, 0)
        ]
        intents = action_view["intents_by_id"]
        receipts = action_view["receipts_by_intent"]
        intent_id = next(iter(intents), None)
        bids = bid_view["bids_by_device"]

        result = {
            "status": "ok" if task_view["status"] == "done" else "incomplete",
            "runtime": {
                "contract": "coordination-v2",
                "process_mode": "single_process_in_memory",
                "provider": self.llm_config.provider,
                "model": self.llm_config.model,
                "physical_gateway": "mock",
                "device_ids": [item.device_id for item in self.devices],
            },
            "task": {
                "task_id": task_payload["task_id"],
                "goal": task_payload["goal"],
                "status": task_view["status"],
                "winner": winner,
                "plan_id": current_plan["plan_id"],
                "completed_assignment_ids": task_view[
                    "completed_assignment_ids"
                ],
            },
            "coordination": {
                "task_events_processed": task_events_processed,
                "agent_bid_events_processed": bid_processed,
                "bid_events_processed_by_coordinator": bid_events_processed,
                "local_model_errors": local_model_errors,
                "group_model_error": observed_group.error,
                "group_candidate": to_json_value(observed_group.candidate),
                "group_candidate_matches_committed": _candidate_matches_committed(
                    observed_group.candidate, current_plan
                ),
                "bids": {
                    device_id: {
                        "offers": record["payload"]["offers"],
                        "proposal": record["payload"].get("proposal"),
                    }
                    for device_id, record in bids.items()
                },
                "assignment_processed_by_agents": assignment_processed,
            },
            "execution": {
                "dispatched_intent_ids_by_device": dispatched_by_device,
                "intent": intents.get(intent_id) if intent_id else None,
                "receipt": receipts.get(intent_id) if intent_id else None,
                "safety_intercepts": action_view["intercepts_by_intent"],
            },
            "completion": {
                "coordinator_processed_events": terminal_events_processed,
                "agent_processed_events": terminal_processed,
                "agent_task_sessions_cleared": sessions_cleared,
                "terminal_event_type": terminal_view["terminal_event_type"],
                "terminal_status": terminal_view["status"],
            },
            "blackboard": {
                "high_watermark": watermark,
                "view_version": task_view["view_version"],
                "events": event_sequence,
            },
        }
        self._assert_closed_loop(result)
        _notify(
            status_listener,
            {
                "stage": "done",
                "message": "任务闭环完成",
                "task_id": task_payload["task_id"],
                "winner": winner,
                "high_watermark": watermark,
            },
        )
        return result

    def _build_hosts(
        self, client: BlackboardClient, work_root: str
    ) -> dict[str, AgentProcessHost]:
        hosts: dict[str, AgentProcessHost] = {}
        for device in self.devices:
            gateway = MockPhysicalActionGateway()
            local_policy = self.local_policy_factory(device.device_id)
            hosts[device.device_id] = AgentProcessHost(
                spec=AgentProcessSpec(
                    device_id=device.device_id,
                    blackboard_connection_ref="memory",
                    work_root=work_root,
                    local_proposal_enabled=local_policy is not None,
                    local_proposal_timeout_s=self.llm_config.timeout_seconds,
                ),
                blackboard=client,
                loop=PureAgentLoop(device.device_id),
                session_store=FileTaskSessionStore(work_root, device.device_id),
                action_executor=ActionExecutor(
                    safety=StaticSafetyPort(),
                    gateway=gateway,
                ),
                action_gateway=gateway,
                skill_provider=NullSkillReferenceProvider(),
                local_proposal_policy=local_policy,
            )
        return hosts

    @staticmethod
    def _attach_llm_listener(
        policy: object, listener: Optional[RuntimeListener]
    ) -> None:
        client = getattr(policy, "client", None)
        if isinstance(client, DeepSeekClient):
            client.telemetry_listener = listener

    @staticmethod
    def _publish_sessions(
        listener: Optional[RuntimeListener],
        hosts: dict[str, AgentProcessHost],
        task_id: str,
        phase: str,
    ) -> None:
        for device_id, host in sorted(hosts.items()):
            session = host.store.load_session(task_id)
            _notify(
                listener,
                {
                    "device_id": device_id,
                    "phase": phase,
                    "exists": session is not None,
                    "session": (
                        to_json_value(session) if session is not None else None
                    ),
                },
            )

    @staticmethod
    def _task_view(client: BlackboardClient, payload: dict) -> dict:
        watermark = client.high_watermark()
        return client.query_view(
            Ledger.TASK.value,
            {
                "view_type": "task_coordination",
                "task_id": payload["task_id"],
                "task_revision": payload["task_revision"],
            },
            watermark,
            0,
        )

    @staticmethod
    def _assert_closed_loop(result: dict) -> None:
        if result["status"] != "ok":
            raise RuntimeError("task did not reach done")
        if result["completion"]["terminal_event_type"] != "task_done":
            raise RuntimeError("TASK_DONE was not committed")
        if not all(
            result["completion"]["agent_task_sessions_cleared"].values()
        ):
            raise RuntimeError("agent task memory was not fully cleaned")
        expected = [
            "task_posted",
            "bid_round_opened",
            "bid",
            "bid",
            "task_assigned",
            "action_intent",
            "receipt",
            "assignment_completed",
            "task_done",
        ]
        actual = [item["type"] for item in result["blackboard"]["events"]]
        if actual != expected:
            raise RuntimeError(f"unexpected event sequence: {actual}")


def _candidate_matches_committed(
    candidate: Optional[CandidateAssignmentPlan], current_plan: dict
) -> bool:
    if candidate is None:
        return False
    candidate_rows = sorted(
        (item.slot_id, item.device_id, item.supporting_bid_event_id)
        for item in candidate.assignments
    )
    committed_rows = sorted(
        (
            str(item.get("slot_id", "")),
            str(item.get("device_id", "")),
            str(item.get("supporting_bid_event_id", "")),
        )
        for item in current_plan.get("assignments", [])
    )
    return candidate_rows == committed_rows


def _notify(listener: Optional[RuntimeListener], payload: dict) -> None:
    if listener is None:
        return
    try:
        listener(payload)
    except Exception:
        # Observability must never change coordination semantics.
        return
