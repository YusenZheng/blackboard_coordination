"""Formal coordination-v2 runtime assembly and observability boundary."""
from __future__ import annotations

import json
import logging
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from uuid import uuid4

from ..access.adapters.mock_adapter import MockAdapter
from ..access.registry import Registry
from ..access.tool_gateway import ToolGateway
from ..access.tools.base import load_builtin_tools
from ..assets.skill import load_builtin_skills
from ..blackboard.board import Blackboard
from ..contracts.agent_card import AgentCard, CapabilitySlot
from ..contracts.blackboard_event import Ledger
from ..contracts.types import CapabilityProfile, DeviceRef, DeviceState, DeviceType
from ..contracts.verbs import ActionVerb
from ..coordination.action_executor import ActionExecutor
from ..coordination.adapters import StaticSafetyPort
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
    SkillQuery,
    SkillReference,
    event_type_value,
    make_blackboard_event,
    to_json_value,
)
from ..coordination.ports import (
    GroupPlanningPolicyPort,
    LocalProposalPolicyPort,
    SkillReferencePort,
)
from ..ingress.task_gen import (
    IntentInterpreterPort,
    TaskGen,
    task_package_to_v2_content,
)
from .deepseek import (
    DeepSeekClient,
    DeepSeekConfig,
    DeepSeekGroupPlanningPolicy,
    DeepSeekIntentInterpreter,
    DeepSeekLocalProposalPolicy,
)
from ..memory import NoOpMemory, TaskEpisodeBuilder
from ..observability import NoopObservability
from .skill_reference_provider import AssetSkillReferenceProvider


logger = logging.getLogger(__name__)


def _blackboard_display_attributes(value: dict[str, Any]) -> dict[str, Any]:
    """Return the small, redaction-safe business projection used by history UI.

    Metadata capture deliberately does not retain a complete event payload.  The
    fields below are therefore copied into structured event attributes so the
    Dashboard can distinguish a missing value from a real ``False`` result.
    ``ObservabilityHub.event`` applies the normal recursive redactor before any
    value reaches SQLite or OpenTelemetry.
    """

    content = value.get("content")
    if not isinstance(content, dict):
        return {}
    event_type = str(value.get("type", ""))
    if event_type == CoordinationEventType.TASK_POSTED.value:
        goal = str(content.get("goal") or "")
        return {
            "task.goal_summary": goal[:256],
            "task.task_type": content.get("task_type"),
            "task.priority": content.get("priority"),
        }
    if event_type == CoordinationEventType.BID_ROUND_OPENED.value:
        slots = content.get("slots")
        return {
            "bid.slots": list(slots[:20]) if isinstance(slots, list) else [],
            "bid.round": content.get("bid_round"),
        }
    if event_type == CoordinationEventType.BID.value:
        offers = content.get("offers")
        first_offer = offers[0] if isinstance(offers, list) and offers else {}
        if not isinstance(first_offer, dict):
            first_offer = {}
        metrics = first_offer.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {}
        proposal = content.get("proposal")
        if not isinstance(proposal, dict):
            proposal = {}
        return {
            "bid.device_id": content.get("device_id"),
            "bid.slot_id": first_offer.get("slot_id"),
            "bid.decision": first_offer.get("decision"),
            "bid.battery_margin": metrics.get("battery_margin"),
            "bid.reliability": metrics.get("reliability"),
            "bid.confidence": proposal.get("confidence"),
        }
    if event_type == CoordinationEventType.TASK_ASSIGNED.value:
        assignments = content.get("assignments")
        first_assignment = (
            assignments[0] if isinstance(assignments, list) and assignments else {}
        )
        if not isinstance(first_assignment, dict):
            first_assignment = {}
        return {
            "assignment.device_id": first_assignment.get("device_id"),
            "assignment.slot_id": first_assignment.get("slot_id"),
            "assignment.count": len(assignments)
            if isinstance(assignments, list)
            else 0,
        }
    if event_type == CoordinationEventType.ACTION_INTENT.value:
        params = content.get("params")
        target = params.get("target") if isinstance(params, dict) else None
        return {
            "action.device_id": content.get("device_id"),
            "action.verb": content.get("verb"),
            "action.target": target,
        }
    if event_type == CoordinationEventType.RECEIPT.value:
        return {
            "receipt.device_id": content.get("device_id"),
            "receipt.success": content.get("success"),
            "receipt.outcome_certainty": content.get("outcome_certainty"),
            "receipt.post_check_allowed": content.get("post_check_allowed"),
            "receipt.failure_reason": content.get("failure_reason"),
        }
    if event_type == CoordinationEventType.SAFETY_INTERCEPT.value:
        return {
            "safety.phase": content.get("check_phase"),
            "safety.risk_level": content.get("risk_level"),
            "safety.reason_code": content.get("reason_code"),
            "safety.reason": content.get("reason"),
        }
    if event_type == CoordinationEventType.ASSIGNMENT_COMPLETED.value:
        return {
            "completion.device_id": content.get("device_id"),
            "completion.progress": content.get("final_progress"),
        }
    if event_type == CoordinationEventType.TASK_DONE.value:
        completed = content.get("completed_assignment_ids")
        return {
            "completion.assignment_count": len(completed)
            if isinstance(completed, list)
            else 0,
            "completion.status": "done",
        }
    if event_type == CoordinationEventType.TASK_FAILED.value:
        return {
            "failure.reason_code": content.get("reason_code"),
            "failure.component": content.get("failed_component"),
            "completion.status": "failed",
        }
    return {}


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


class ObservedIntentInterpreter:
    """Instrument the model boundary without changing interpreter semantics."""

    def __init__(self, inner: IntentInterpreterPort, observability: Any) -> None:
        self.inner = inner
        self.observability = observability

    def interpret(self, raw_input: str, sequence: int) -> dict:
        with self.observability.span(
            "gen_ai.intent.interpret",
            attributes={"gen_ai.operation": "intent_interpret"},
            input_payload={"raw_input": raw_input, "sequence": sequence},
        ) as span:
            try:
                result = self.inner.interpret(raw_input, sequence)
                span.set_output(result)
                return result
            except Exception as exc:
                span.record_exception(exc)
                raise


class ObservedLocalProposalPolicy:
    """Per-Agent local model boundary."""

    def __init__(self, device_id: str, inner: Any, observability: Any) -> None:
        self.device_id = device_id
        self.inner = inner
        self.observability = observability

    def propose(self, context: dict, timeout_s: float):
        with self.observability.span(
            "gen_ai.local_proposal",
            attributes={
                "agent.id": self.device_id,
                "device.id": self.device_id,
                "gen_ai.operation": "local_proposal",
            },
            input_payload=context,
        ) as span:
            try:
                result = self.inner.propose(context, timeout_s)
                span.set_output(result)
                return result
            except Exception as exc:
                span.record_exception(exc)
                raise


class ObservedGroupPlanningPolicy:
    """Record safe policy diagnostics while preserving Coordinator fallback."""

    def __init__(
        self,
        inner: GroupPlanningPolicyPort,
        observability: Any,
        memory: Any,
    ) -> None:
        self.inner = inner
        self.observability = observability
        self.memory = memory
        self.candidate: Optional[CandidateAssignmentPlan] = None
        self.error: Optional[str] = None

    def plan(self, planning_input, timeout_s: float) -> CandidateAssignmentPlan:
        query = " ".join(
            str(value)
            for value in (
                planning_input.task.get("goal", ""),
                planning_input.task.get("task_type", ""),
            )
            if value
        )
        with self.observability.span(
            "memory.retrieve",
            attributes={"memory.scope": "shared", "memory.fact_limit": 5},
            input_payload={"query": query},
        ) as memory_span:
            try:
                shared = self.memory.retrieve_shared_context(
                    query,
                    fact_limit=5,
                    max_chars=4000,
                )
                planning_input.shared_memory = []
                used_chars = 0
                for record in shared.get("shared_facts", []):
                    compact = {
                        key: record.get(key)
                        for key in (
                            "id",
                            "topic",
                            "fact",
                            "confidence",
                            "expires_at",
                            "source_trace_id",
                            "source_event_ids",
                        )
                        if record.get(key) is not None
                    }
                    rendered = json.dumps(
                        to_json_value(compact),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if used_chars + len(rendered) > 4000:
                        continue
                    planning_input.shared_memory.append(compact)
                    used_chars += len(rendered)
                memory_span.set_attribute(
                    "memory.hit_count", len(planning_input.shared_memory)
                )
                memory_span.set_attribute(
                    "memory.character_count", used_chars
                )
                memory_span.set_output(shared)
            except Exception as exc:
                memory_span.record_exception(exc)
                planning_input.shared_memory = []
        with self.observability.span(
            "gen_ai.group_plan",
            attributes={
                "gen_ai.operation": "group_planning",
                "bid.count": len(planning_input.bids),
            },
            input_payload=planning_input,
        ) as span:
            try:
                self.candidate = self.inner.plan(planning_input, timeout_s)
                span.set_output(self.candidate)
                return self.candidate
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                span.record_exception(exc)
                raise


class ObservedSkillReferenceProvider:
    """Expose the actual Asset Skill references supplied to one v2 Agent."""

    def __init__(
        self,
        device_id: str,
        inner: SkillReferencePort,
    ) -> None:
        self.device_id = device_id
        self.inner = inner
        self.references: list[SkillReference] = []

    def search(self, query: SkillQuery, limit: int = 3) -> list[SkillReference]:
        self.references = list(self.inner.search(query, limit))
        return list(self.references)


LocalPolicyFactory = Callable[[str], Optional[LocalProposalPolicyPort]]
RuntimeListener = Callable[[dict], None]


class CoordinationRuntime:
    """Execute one coordination-v2 task from instruction through TASK_DONE."""

    def __init__(
        self,
        *,
        deepseek_config: Optional[DeepSeekConfig] = None,
        intent_interpreter: Optional[IntentInterpreterPort] = None,
        local_policy_factory: Optional[LocalPolicyFactory] = None,
        group_policy: Optional[GroupPlanningPolicyPort] = None,
        devices: Optional[list[DeviceRuntimeConfig]] = None,
        bid_window_s: float = 30.0,
        observability: Any = None,
        memory: Any = None,
    ) -> None:
        if bid_window_s <= 0:
            raise ValueError("bid_window_s must be positive")
        self.deepseek_config = deepseek_config or DeepSeekConfig.from_env()
        self.observability = (
            observability if observability is not None else NoopObservability()
        )
        self.memory = memory if memory is not None else NoOpMemory()
        configure_logging = getattr(self.observability, "configure_logging", None)
        if callable(configure_logging):
            try:
                configure_logging(logging.getLogger("swarm_brain"))
            except Exception:
                pass
        self._llm_listener: Optional[RuntimeListener] = None
        base_interpreter = intent_interpreter or DeepSeekIntentInterpreter(
            DeepSeekClient(self.deepseek_config)
        )
        self.intent_interpreter = ObservedIntentInterpreter(
            base_interpreter, self.observability
        )
        self.local_policy_factory = local_policy_factory or (
            lambda _device_id: DeepSeekLocalProposalPolicy(
                DeepSeekClient(
                    self.deepseek_config,
                    telemetry_listener=self._llm_listener,
                )
            )
        )
        self.group_policy = group_policy or DeepSeekGroupPlanningPolicy(
            DeepSeekClient(self.deepseek_config)
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
        tool_listener: Optional[RuntimeListener] = None,
        capture_mode: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> dict:
        if not instruction.strip():
            raise ValueError("instruction is required")
        resolved_run_id = self.observability.start_run(
            run_id or str(uuid4()),
            capture_mode=capture_mode,
            attributes={"runtime.contract": "coordination-v2"},
        )
        started = time.perf_counter()
        failure: Optional[BaseException] = None
        result: Optional[dict] = None
        root_trace_context: dict[str, str] = {}

        observed_event_listener = self._observation_listener(
            "blackboard.event.committed",
            event_listener,
            category="blackboard",
        )
        observed_status_listener = self._observation_listener(
            "runtime.status",
            status_listener,
            category="status",
        )
        observed_llm_listener = self._llm_observation_listener(llm_listener)
        observed_session_listener = self._observation_listener(
            "session.snapshot",
            session_listener,
            category="session",
        )
        observed_tool_listener = self._observation_listener(
            "tool.result",
            tool_listener,
            category="tool",
        )

        try:
            with self.observability.span(
                "swarm_brain.chain1.run",
                attributes={
                    "run.id": resolved_run_id,
                    "runtime.contract": "coordination-v2",
                },
                input_payload={"instruction": instruction},
            ) as root_span:
                root_trace_context = dict(
                    getattr(root_span, "span_context", {}) or {}
                )
                logger.info(
                    "V2 链路1开始执行",
                    extra={
                        "operation": "chain1.run.started",
                        "observation_attributes": {
                            "status": "running",
                            "duration_ms": 0.0,
                            "task_id": None,
                            "winner": None,
                            "runtime_contract": "coordination-v2",
                        },
                    },
                )
                try:
                    result = self._run_chain(
                        instruction,
                        event_listener=observed_event_listener,
                        status_listener=observed_status_listener,
                        llm_listener=observed_llm_listener,
                        session_listener=observed_session_listener,
                        tool_listener=observed_tool_listener,
                        run_id=resolved_run_id,
                    )
                    task_result = result.get("task", {})
                    root_span.set_attribute(
                        "task.id", str(task_result.get("task_id", ""))
                    )
                    root_span.set_attribute(
                        "task.status", str(task_result.get("status", ""))
                    )
                    root_span.set_output(
                        {
                            "status": result.get("status"),
                            "task": task_result,
                            "blackboard": {
                                "high_watermark": result.get(
                                    "blackboard", {}
                                ).get("high_watermark")
                            },
                        }
                    )
                    trace_context = dict(
                        getattr(root_span, "span_context", {}) or {}
                    )
                    result["run_id"] = resolved_run_id
                    result["trace_id"] = trace_context.get("trace_id")
                    result["observability"] = {
                        "run_id": resolved_run_id,
                        "trace_id": trace_context.get("trace_id"),
                        "capture_mode": capture_mode
                        or getattr(
                            self.observability, "capture_mode", "disabled"
                        ),
                        "health": self._observability_health(),
                    }
                    result["memory"] = self._consolidate_memory(
                        resolved_run_id,
                        trace_context,
                        result,
                    )
                    self._record_observability_health_metrics()
                    health = self._observability_health()
                    task_id = str(task_result.get("task_id", ""))
                    winner = task_result.get("winner")
                    duration_ms = (time.perf_counter() - started) * 1000.0
                    if health.get("degraded"):
                        logger.warning(
                            "可观测存储处于降级状态，V2 业务结果不受影响",
                            extra={
                                "operation": "observability.degraded",
                                "task_id": task_id or None,
                                "device_id": winner,
                                "observation_attributes": {
                                    "status": "degraded",
                                    "duration_ms": duration_ms,
                                    "task_id": task_id or None,
                                    "winner": winner,
                                    "dropped": health.get("dropped", 0),
                                    "write_errors": health.get(
                                        "write_errors", 0
                                    ),
                                },
                            },
                        )
                    logger.info(
                        "V2 链路1执行完成",
                        extra={
                            "operation": "chain1.run.completed",
                            "task_id": task_id or None,
                            "device_id": winner,
                            "observation_attributes": {
                                "status": str(result.get("status", "ok")),
                                "duration_ms": duration_ms,
                                "task_id": task_id or None,
                                "winner": winner,
                                "memory_status": result.get("memory", {}).get(
                                    "status"
                                ),
                            },
                        },
                    )
                except Exception as exc:
                    failure = exc
                    root_span.record_exception(exc)
                    failed_task = (result or {}).get("task", {})
                    failed_task_id = failed_task.get("task_id")
                    failed_winner = failed_task.get("winner")
                    logger.exception(
                        "V2 链路1执行失败",
                        extra={
                            "operation": "chain1.run.failed",
                            "task_id": failed_task_id,
                            "device_id": failed_winner,
                            "error_type": type(exc).__name__,
                            "observation_attributes": {
                                "status": "failed",
                                "duration_ms": (
                                    time.perf_counter() - started
                                )
                                * 1000.0,
                                "task_id": failed_task_id,
                                "winner": failed_winner,
                            },
                        },
                    )
                    raise
        finally:
            status = (
                "failed"
                if failure is not None
                else str((result or {}).get("status", "incomplete"))
            )
            try:
                self.observability.end_run(
                    resolved_run_id,
                    status=status,
                    error=failure,
                    attributes={
                        "duration_ms": (
                            time.perf_counter() - started
                        )
                        * 1000.0
                    },
                )
                self.observability.flush(timeout_s=2.0)
            except Exception as exc:
                # Observability is explicitly fail-open.
                logger.exception(
                    "可观测数据落盘失败，V2 业务结果不受影响",
                    extra={
                        "operation": "observability.flush",
                        "run_id": resolved_run_id,
                        "trace_id": root_trace_context.get("trace_id"),
                        "span_id": root_trace_context.get("span_id"),
                        "error_type": type(exc).__name__,
                        "observation_attributes": {
                            "status": "degraded",
                            "duration_ms": (
                                time.perf_counter() - started
                            )
                            * 1000.0,
                            "task_id": (result or {}).get("task", {}).get(
                                "task_id"
                            ),
                            "winner": (result or {}).get("task", {}).get(
                                "winner"
                            ),
                        },
                    },
                )
        if result is None:
            raise RuntimeError("coordination-v2 run returned no result")
        return result

    def _run_chain(
        self,
        instruction: str,
        *,
        event_listener: Optional[RuntimeListener] = None,
        status_listener: Optional[RuntimeListener] = None,
        llm_listener: Optional[RuntimeListener] = None,
        session_listener: Optional[RuntimeListener] = None,
        tool_listener: Optional[RuntimeListener] = None,
        run_id: str,
    ) -> dict:
        self._llm_listener = llm_listener
        self._attach_llm_listener(self.intent_interpreter, llm_listener)
        self._attach_llm_listener(self.group_policy, llm_listener)
        tool_traces: list[dict] = []

        def record_tool_trace(payload: dict) -> None:
            value = to_json_value(payload)
            tool_traces.append(value)
            _notify(tool_listener, value)

        _notify(
            status_listener,
            {
                "stage": "runtime_starting",
                "message": "初始化 Blackboard 与设备运行时",
            },
        )
        clock = RuntimeClock()
        board = Blackboard(clock=clock)
        client = BlackboardClient(board, observability=self.observability)
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
                        "task_id": event.content.get("task_id"),
                        "task_revision": event.content.get("task_revision"),
                        "coordination_epoch": event.content.get(
                            "coordination_epoch"
                        ),
                        "agent_id": event.content.get(
                            "agent_id", event.content.get("device_id")
                        ),
                        "device_id": event.content.get("device_id"),
                    },
                )
            )
        for device in self.devices:
            board.upsert_agent_snapshot(device.public_snapshot(clock.value))

        _notify(
            status_listener,
            {
                "stage": "intent_interpreting",
                "message": "DeepSeek 正在把用户指令转换为结构化任务",
            },
        )
        with self.observability.span(
            "ingress.task.generate",
            attributes={"run.id": run_id},
            input_payload={"instruction": instruction},
        ) as span:
            try:
                task = self.task_gen.generate(instruction)
                task_payload = task_package_to_v2_content(task)
                span.set_output(task_payload)
            except Exception as exc:
                span.record_exception(exc)
                raise
        update_run = getattr(self.observability, "update_run", None)
        if callable(update_run):
            try:
                update_run(
                    run_id,
                    task_id=task_payload["task_id"],
                    task_revision=task_payload["task_revision"],
                    coordination_epoch=0,
                )
            except Exception:
                pass
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

        observed_group = ObservedGroupPlanningPolicy(
            self.group_policy,
            self.observability,
            self.memory,
        )
        with tempfile.TemporaryDirectory(
            # Keep the durable outbox path below legacy Win32 MAX_PATH.
            prefix="sb-v2-"
        ) as work_root:
            coordinator = Coordinator(
                blackboard=client,
                store=FileCoordinatorStore(work_root),
                group_policy=observed_group,
                bid_window_s=self.bid_window_s,
                group_policy_timeout_s=self.deepseek_config.timeout_s,
                observability=self.observability,
            )
            hosts = self._build_hosts(
                client,
                work_root,
                tool_trace_listener=record_tool_trace,
            )

            _notify(
                status_listener,
                {
                    "stage": "bid_round_opening",
                    "message": "Coordinator 正在打开竞标轮次",
                },
            )
            bid_open_started = time.perf_counter()
            with self.observability.span(
                "coordinator.bid_round.open",
                attributes={
                    "task.id": task_payload["task_id"],
                    "task.revision": task_payload["task_revision"],
                },
            ) as span:
                task_events_processed = coordinator.poll_once(timeout_s=0)
                span.set_output({"events_processed": task_events_processed})
            self.observability.histogram(
                "coordinator.bid_round.open.duration_ms",
                (time.perf_counter() - bid_open_started) * 1000.0,
                attributes={"status": "ok"},
            )
            _notify(
                status_listener,
                {
                    "stage": "agents_bidding",
                    "message": "dog-a 与 dog-b 正在独立分析并报价",
                },
            )
            bid_processed = {}
            for device_id, host in hosts.items():
                bid_started = time.perf_counter()
                bid_processed[device_id] = host.poll_once(timeout_s=0)
                self.observability.histogram(
                    "agent.bid.duration_ms",
                    (time.perf_counter() - bid_started) * 1000.0,
                    attributes={"status": "ok"},
                )
                self.observability.counter(
                    "agent.bid.count",
                    attributes={"status": "processed"},
                )
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
            bid_links = [
                host.last_bid_span_context
                for host in hosts.values()
                if host.last_bid_span_context
            ]
            group_started = time.perf_counter()
            with self.observability.span(
                "coordinator.group_plan",
                attributes={
                    "task.id": task_payload["task_id"],
                    "task.revision": task_payload["task_revision"],
                    "bid.branch_count": len(bid_links),
                },
                links=bid_links,
            ) as span:
                clock.advance(self.bid_window_s + 1.0)
                close_outputs = coordinator.tick()
                span.set_output(
                    {
                        "output_event_types": [
                            event_type_value(item) for item in close_outputs
                        ]
                    }
                )
            self.observability.histogram(
                "coordinator.group_plan.duration_ms",
                (time.perf_counter() - group_started) * 1000.0,
                attributes={
                    "status": "fallback"
                    if observed_group.error
                    else "ok"
                },
            )
            if [event_type_value(item) for item in close_outputs] != [
                CoordinationEventType.TASK_ASSIGNED.value
            ]:
                raise RuntimeError("bid round did not converge to TASK_ASSIGNED")

            assigned_view = self._task_view(client, task_payload)
            current_plan = assigned_view.get("current_plan")
            if not isinstance(current_plan, dict):
                raise RuntimeError("TASK_ASSIGNED view has no committed plan")
            if callable(update_run):
                try:
                    update_run(
                        run_id,
                        coordination_epoch=int(
                            current_plan.get("coordination_epoch", 0)
                        ),
                    )
                except Exception:
                    pass
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
                    assignment_started = time.perf_counter()
                    assignment_processed[device_id] = hosts[device_id].poll_once(
                        timeout_s=0
                    )
                    self.observability.histogram(
                        "agent.assignment.duration_ms",
                        (time.perf_counter() - assignment_started) * 1000.0,
                        attributes={"role": "observer"},
                    )
            _notify(
                status_listener,
                {
                    "stage": "executing",
                    "message": f"{winner} 正在执行受限动作并等待回执",
                    "winner": winner,
                },
            )
            assignment_started = time.perf_counter()
            assignment_processed[winner] = hosts[winner].poll_once(timeout_s=0)
            self.observability.histogram(
                "agent.assignment.duration_ms",
                (time.perf_counter() - assignment_started) * 1000.0,
                attributes={"role": "winner"},
            )
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
            with self.observability.span(
                "coordinator.complete",
                attributes={
                    "task.id": task_payload["task_id"],
                    "winner": winner,
                },
            ) as span:
                terminal_events_processed = coordinator.poll_once(timeout_s=0)
                span.set_output(
                    {"events_processed": terminal_events_processed}
                )
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
            skill_references_by_device = {
                device_id: to_json_value(host.skill_provider.references)
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
                "model": self.deepseek_config.model,
                "physical_gateway": "whx_tool_runtime",
                "physical_adapter": "mock",
                "skill_provider": "asset_skill_reference_provider",
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
                "tool_calls": tool_traces,
                "skill_references_by_device": skill_references_by_device,
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

    def _observation_listener(
        self,
        name: str,
        downstream: Optional[RuntimeListener],
        *,
        category: str,
    ) -> RuntimeListener:
        def listener(payload: dict) -> None:
            value = to_json_value(payload)
            attributes = {
                "category": category,
                "operation": value.get("operation")
                or value.get("stage")
                or name,
            }
            for source_key, target_key in (
                ("event_id", "event.id"),
                ("type", "event.type"),
                ("source", "event.source"),
                ("offset", "blackboard.offset"),
                ("version", "blackboard.version"),
                ("task_id", "task.id"),
                ("task_revision", "task.revision"),
                ("coordination_epoch", "coordination.epoch"),
                ("agent_id", "agent.id"),
                ("device_id", "device.id"),
                ("intent_id", "intent.id"),
                ("call_id", "tool.call.id"),
                ("error_type", "error.type"),
                ("status", "status"),
                ("success", "success"),
            ):
                item = value.get(source_key)
                if item is not None:
                    attributes[target_key] = item
            if category == "blackboard":
                attributes.update(_blackboard_display_attributes(value))
            level = (
                "ERROR"
                if value.get("error_type")
                or value.get("status") == "error"
                else "INFO"
            )
            try:
                self.observability.event(
                    name,
                    attributes=attributes,
                    payload=value.get("content", value),
                    level=level,
                )
                if category == "blackboard":
                    self.observability.counter(
                        "blackboard.event.count",
                        attributes={
                            "event.type": str(value.get("type", "unknown"))
                        },
                    )
                    if value.get("type") == "safety_intercept":
                        self.observability.counter(
                            "safety.intercept.count",
                            attributes={
                                "phase": str(
                                    value.get("content", {}).get(
                                        "check_phase", "unknown"
                                    )
                                )
                            },
                        )
            except Exception:
                pass
            self._notify_subscriber(downstream, value, category)

        return listener

    def _llm_observation_listener(
        self, downstream: Optional[RuntimeListener]
    ) -> RuntimeListener:
        def listener(payload: dict) -> None:
            value = to_json_value(payload)
            operation = str(value.get("operation", "unknown"))
            model = str(value.get("model", "unknown"))
            status = str(value.get("status", "unknown"))
            metric_attributes = {
                "operation": operation,
                "model": model,
                "status": status,
                "error_type": str(value.get("error_type") or "none"),
            }
            try:
                self.observability.event(
                    "gen_ai.call",
                    attributes={
                        "category": "llm",
                        "gen_ai.operation": operation,
                        "gen_ai.model": model,
                        "status": status,
                        "error.type": value.get("error_type"),
                        "duration_ms": value.get("duration_ms"),
                        "input_tokens": value.get("input_tokens"),
                        "output_tokens": value.get("output_tokens"),
                        "total_tokens": value.get("total_tokens"),
                        "cache_hit_tokens": value.get("cache_hit_tokens"),
                        "cache_miss_tokens": value.get("cache_miss_tokens"),
                        "fallback": status != "ok",
                    },
                    level="ERROR" if status == "error" else "INFO",
                )
                self.observability.counter(
                    "gen_ai.call.count", attributes=metric_attributes
                )
                if value.get("duration_ms") is not None:
                    self.observability.histogram(
                        "gen_ai.call.duration_ms",
                        float(value["duration_ms"]),
                        attributes=metric_attributes,
                    )
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cache_hit_tokens",
                    "cache_miss_tokens",
                    "reasoning_tokens",
                ):
                    if value.get(key) is not None:
                        self.observability.counter(
                            "gen_ai.token.count",
                            int(value[key]),
                            attributes={
                                "operation": operation,
                                "model": model,
                                "token.kind": key,
                            },
                        )
            except Exception:
                pass
            self._notify_subscriber(downstream, value, "llm")

        return listener

    def _notify_subscriber(
        self,
        listener: Optional[RuntimeListener],
        payload: dict,
        channel: str,
    ) -> None:
        if listener is None:
            return
        try:
            listener(payload)
        except Exception as exc:
            # Realtime UI/listener failures are observable but never become
            # coordination failures.
            try:
                with self.observability.span(
                    "subscriber.callback",
                    attributes={
                        "subscriber.channel": channel,
                        "error.type": type(exc).__name__,
                    },
                ) as span:
                    span.record_exception(exc)
            except Exception:
                pass

    def _observability_health(self) -> dict:
        health = getattr(self.observability, "health", None)
        if not callable(health):
            return {"status": "unknown", "degraded": False}
        try:
            return dict(health())
        except Exception as exc:
            return {
                "status": "degraded",
                "degraded": True,
                "error_type": type(exc).__name__,
            }

    def _record_observability_health_metrics(self) -> None:
        health = self._observability_health()
        try:
            self.observability.histogram(
                "observability.queue.depth",
                float(health.get("queue_depth", 0)),
                attributes={
                    "status": str(health.get("status", "unknown"))
                },
            )
            self.observability.counter(
                "observability.records.dropped",
                float(health.get("dropped", 0)),
                attributes={
                    "status": str(health.get("status", "unknown"))
                },
            )
            self.observability.counter(
                "observability.sqlite.write_errors",
                float(health.get("write_errors", 0)),
                attributes={
                    "status": str(health.get("status", "unknown"))
                },
            )
        except Exception:
            pass

    def _consolidate_memory(
        self,
        run_id: str,
        trace_context: dict[str, str],
        result: dict,
    ) -> dict:
        consolidate = getattr(self.memory, "consolidate_trace_asset", None)
        query_trace = getattr(self.observability, "query_trace", None)
        query_timeline = getattr(self.observability, "query_timeline", None)
        if not all(callable(item) for item in (consolidate, query_trace, query_timeline)):
            return {"status": "disabled"}
        task = result.get("task", {})
        task_id = str(task.get("task_id", ""))
        with self.observability.span(
            "memory.consolidate",
            attributes={
                "task.id": task_id,
                "memory.operation": "trace_asset_consolidation",
            },
        ) as span:
            try:
                # Child spans and committed timeline events are safe to flush
                # while the root span remains active.
                self.observability.flush(timeout_s=0.75)
                stored_spans = list(query_trace(run_id))
                timeline = list(query_timeline(run_id))
                root_span_id = str(trace_context.get("span_id", ""))
                if root_span_id and not any(
                    str(item.get("span_id", "")) == root_span_id
                    for item in stored_spans
                ):
                    stored_spans.insert(
                        0,
                        {
                            "span_id": root_span_id,
                            "trace_id": trace_context.get("trace_id", ""),
                            "parent_span_id": None,
                            "name": "swarm_brain.chain1.run",
                            "start_time": time.time(),
                        },
                    )
                business_events: list[dict] = []
                for item in timeline:
                    attributes = dict(item.get("attributes") or {})
                    event_type = attributes.get("event.type")
                    event_id = attributes.get("event.id")
                    if not event_type or not event_id:
                        continue
                    business_events.append(
                        {
                            "event_id": event_id,
                            "event_type": event_type,
                            "offset": attributes.get("blackboard.offset"),
                            "agent_id": attributes.get("agent.id"),
                            "device_id": attributes.get("device.id"),
                            "span_id": item.get("span_id"),
                            "timestamp": item.get("timestamp"),
                        }
                    )
                asset = TaskEpisodeBuilder().build(
                    run_id=run_id,
                    task_id=task_id,
                    trace_id=str(trace_context.get("trace_id", "")),
                    events=business_events,
                    spans=stored_spans,
                    terminal_status=str(
                        result.get("completion", {}).get(
                            "terminal_status", result.get("status", "")
                        )
                    ),
                    participant_agent_ids=[
                        item.device_id for item in self.devices
                    ],
                )
                receipt = result.get("execution", {}).get("receipt") or {}
                fact_candidates: list[dict] = []
                if (
                    receipt.get("success")
                    and receipt.get("outcome_certainty", "confirmed")
                    == "confirmed"
                ):
                    fact_candidates.append(
                        {
                            "topic": f"task:{task_id}:outcome",
                            "fact": {
                                "task_id": task_id,
                                "winner": task.get("winner"),
                                "status": task.get("status"),
                            },
                            "source_type": "confirmed_receipt",
                            "confidence": 1.0,
                            "source_event_ids": [
                                item["event_id"]
                                for item in business_events
                                if item["event_type"] == "receipt"
                            ],
                        }
                    )
                consolidated = consolidate(
                    asset,
                    fact_candidates=fact_candidates,
                )
                consolidated_value = (
                    consolidated.to_dict()
                    if hasattr(consolidated, "to_dict")
                    else to_json_value(consolidated)
                )
                save_asset = getattr(
                    self.observability, "save_trace_asset", None
                )
                if callable(save_asset):
                    asset_value = asset.to_dict()
                    asset_value["quality_status"] = consolidated_value.get(
                        "quality_status", "raw"
                    )
                    save_asset(asset_value)
                span.set_attribute(
                    "memory.private_promotions",
                    len(consolidated_value.get("private_memory_ids", [])),
                )
                span.set_attribute(
                    "memory.candidate_promotions",
                    len(consolidated_value.get("candidate_ids", [])),
                )
                span.set_output(consolidated_value)
                self.observability.counter(
                    "memory.promotion.count",
                    len(consolidated_value.get("private_memory_ids", [])),
                    attributes={
                        "kind": "private_episodic",
                        "quality_status": str(
                            consolidated_value.get(
                                "quality_status", "unknown"
                            )
                        ),
                    },
                )
                return {
                    "status": "consolidated",
                    **consolidated_value,
                }
            except Exception as exc:
                span.record_exception(exc)
                logger.warning(
                    "Trace 记忆固化降级，V2 业务结果不受影响",
                    exc_info=True,
                    extra={
                        "operation": "memory.consolidate",
                        "task_id": task_id or None,
                        "error_type": type(exc).__name__,
                        "observation_attributes": {
                            "status": "degraded",
                            "task_id": task_id or None,
                        },
                    },
                )
                return {
                    "status": "degraded",
                    "error_type": type(exc).__name__,
                }

    def _build_hosts(
        self,
        client: BlackboardClient,
        work_root: str,
        *,
        tool_trace_listener: Optional[RuntimeListener] = None,
    ) -> dict[str, AgentProcessHost]:
        tool_catalog = load_builtin_tools()
        device_registry = Registry(emit_console=False)
        for device in self.devices:
            device_registry.register(
                _device_agent_card(device, tool_catalog)
            )
        skill_provider = AssetSkillReferenceProvider(
            load_builtin_skills(),
            tool_catalog,
        )

        hosts: dict[str, AgentProcessHost] = {}
        for device in self.devices:
            gateway = ToolGateway(
                adapters={
                    device.device_id: MockAdapter(
                        device.device_id,
                        emit_console=False,
                    )
                },
                tool_registry=tool_catalog,
                device_registry=device_registry,
                blackboard=client,
                trace_listener=tool_trace_listener,
                observability=self.observability,
            )
            local_policy = self.local_policy_factory(device.device_id)
            if local_policy is not None:
                self._attach_llm_listener(local_policy, self._llm_listener)
                local_policy = ObservedLocalProposalPolicy(
                    device.device_id,
                    local_policy,
                    self.observability,
                )
            hosts[device.device_id] = AgentProcessHost(
                spec=AgentProcessSpec(
                    device_id=device.device_id,
                    blackboard_connection_ref="memory",
                    work_root=work_root,
                    local_proposal_enabled=local_policy is not None,
                    local_proposal_timeout_s=self.deepseek_config.timeout_s,
                ),
                blackboard=client,
                loop=PureAgentLoop(device.device_id),
                session_store=FileTaskSessionStore(work_root, device.device_id),
                action_executor=ActionExecutor(
                    safety=StaticSafetyPort(),
                    gateway=gateway,
                    observability=self.observability,
                ),
                action_gateway=gateway,
                skill_provider=ObservedSkillReferenceProvider(
                    device.device_id,
                    skill_provider,
                ),
                local_proposal_policy=local_policy,
                observability=self.observability,
                memory=self.memory,
            )
        return hosts

    @staticmethod
    def _attach_llm_listener(
        policy: object, listener: Optional[RuntimeListener]
    ) -> None:
        current: Any = policy
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            client = getattr(current, "client", None)
            if isinstance(client, DeepSeekClient):
                client.telemetry_listener = listener
                return
            current = getattr(current, "inner", None)

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


def _device_agent_card(device: DeviceRuntimeConfig, tool_catalog) -> AgentCard:
    action_verbs = [ActionVerb(value) for value in device.action_verbs]
    atomic_tools = [
        spec.tool_id
        for spec in tool_catalog.list()
        if spec.action_verb in device.action_verbs
    ]
    return AgentCard(
        identity=DeviceRef(device.device_id, DeviceType.DOG),
        state=DeviceState(
            battery=device.battery,
            endurance_s=device.endurance_s,
            online=True,
            healthy=True,
        ),
        capability=CapabilitySlot(
            action_verbs=action_verbs,
            atomic_tools=atomic_tools,
            profile=CapabilityProfile(
                capabilities=list(device.capabilities),
                width_cm=40,
            ),
        ),
    )


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
