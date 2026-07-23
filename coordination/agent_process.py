# STATUS: STAGED(A类)—— 一设备一进程 Host、FIFO 和 Supervisor
"""AgentProcess I/O 宿主。

Host 负责 I/O、上下文装配、Effect 执行和消费位置提交；PureAgentLoop 只做同步状态
转换。Supervisor 只管理进程生命周期，不参与 BID、组队或动作判断。
"""
from __future__ import annotations

import multiprocessing
import os
import queue
import time
from dataclasses import asdict
from typing import Any, Callable, Optional

from ..contracts.blackboard_event import BlackboardEvent, Ledger
from .action_executor import ActionExecutor
from .file_session_store import FileTaskSessionStore
from .models import (
    AgentProcessSpec,
    AgentPublicSnapshot,
    AppendStatus,
    CollaborationProposal,
    CoordinationEventType,
    EffectKind,
    EventEnvelope,
    ExecutionAvailability,
    IntentState,
    LoopInput,
    ProcessState,
    ProcessStatus,
    RoleSlot,
    SkillQuery,
    TaskSession,
    event_content,
    event_type_value,
    fingerprint,
    to_json_value,
)
from .ports import (
    BlackboardClientPort,
    LocalProposalPolicyPort,
    PhysicalActionGatewayPort,
    PureAgentLoopPort,
    SkillReferencePort,
)
from .publisher import DurableEventPublisher


AGENT_EVENT_TYPES = {
    CoordinationEventType.BID_ROUND_OPENED.value,
    CoordinationEventType.TASK_ASSIGNED.value,
    CoordinationEventType.ACTION_INTENT.value,
    CoordinationEventType.RECEIPT.value,
    CoordinationEventType.SAFETY_INTERCEPT.value,
    CoordinationEventType.ASSIGNMENT_COMPLETED.value,
    CoordinationEventType.TASK_REPLAN.value,
    CoordinationEventType.ESTOP.value,
    CoordinationEventType.TASK_DONE.value,
    CoordinationEventType.TASK_FAILED.value,
}


class SimpleMailbox:
    def __init__(self, maxsize: int = 100) -> None:
        self._queue: queue.Queue[EventEnvelope] = queue.Queue(maxsize=maxsize)

    def put(self, envelope: EventEnvelope) -> bool:
        try:
            self._queue.put_nowait(envelope)
            return True
        except queue.Full:
            return False

    def get(self) -> Optional[EventEnvelope]:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def __len__(self) -> int:
        return self._queue.qsize()


class AgentProcessHost:
    def __init__(
        self,
        *,
        spec: AgentProcessSpec,
        blackboard: BlackboardClientPort,
        loop: PureAgentLoopPort,
        session_store: FileTaskSessionStore,
        action_executor: ActionExecutor,
        action_gateway: PhysicalActionGatewayPort,
        skill_provider: SkillReferencePort,
        local_proposal_policy: Optional[LocalProposalPolicyPort] = None,
        mailbox_size: int = 100,
        view_timeout_s: float = 3.0,
    ) -> None:
        self.spec = spec
        self.blackboard = blackboard
        self.loop = loop
        self.store = session_store
        self.action_executor = action_executor
        self.action_gateway = action_gateway
        self.skill_provider = skill_provider
        self.local_proposal_policy = local_proposal_policy
        self.mailbox = SimpleMailbox(mailbox_size)
        self.view_timeout_s = view_timeout_s
        self.last_local_proposal_error: Optional[str] = None
        self._running = False
        self._current_input_offset: Optional[int] = None
        self._pending_commit_keys: list[tuple[str, str]] = []
        self.action_executor.publish_event = self._publish_task_event
        self.action_executor.store_raw_receipt = self.store.store_raw_receipt
        # raw receipt 只能在标准 RECEIPT、Session 和消费位置全部提交后删除。
        self.action_executor.remove_raw_receipt = None
        self.action_executor.on_dispatching = self._mark_dispatching

    def poll_once(self, *, limit: int = 100, timeout_s: float = 0.1) -> int:
        last_offset = self.store.load_consumer_offset()
        envelopes = self.blackboard.read_since(
            last_offset, AGENT_EVENT_TYPES, limit, timeout_s
        )
        for value in envelopes:
            envelope = _normalize_envelope(value)
            if not self.mailbox.put(envelope):
                break
        processed = 0
        while len(self.mailbox):
            envelope = self.mailbox.get()
            if envelope is None or not self._handle_envelope(envelope):
                break
            processed += 1
        return processed

    def run_forever(self, *, poll_timeout_s: float = 0.5) -> None:
        self._running = True
        self.replay_outbox()
        while self._running:
            self.poll_once(timeout_s=poll_timeout_s)

    def stop(self) -> None:
        self._running = False

    def replay_outbox(self) -> int:
        replayed = 0
        committed_offset = self.store.load_consumer_offset()
        for record in self.store.list_all_task_event_records():
            # source_offset 尚未提交的输出必须由对应输入事件重新处理；这样 Session
            # 和 offset 才能与输出一起收敛。已提交后遗留的 outbox 可安全清理。
            if record.source_offset is not None and record.source_offset > committed_offset:
                continue
            event = record.event
            task_id = str(event_content(event).get("task_id", ""))
            if not task_id or not event.idempotency_key:
                continue
            publisher = self._publisher_for_task(task_id, record.source_offset)
            result = publisher.publish(event)
            if result.status in (AppendStatus.APPENDED, AppendStatus.DUPLICATE_SAME):
                publisher.resolve(event.idempotency_key)
            replayed += 1
        return replayed

    def _handle_envelope(self, envelope: EventEnvelope) -> bool:
        event = envelope.event
        content = event_content(event)
        task_id = str(content.get("task_id", ""))
        if not task_id:
            # ESTOP(scope=all) 没有 task_id 时由 Coordinator 负责失败收敛；Host 只留消费痕迹。
            self.store.save_consumer_offset(envelope.offset)
            return True
        task_revision = int(content.get("task_revision", 1))
        session = self.store.load_session(task_id) or TaskSession.observed(
            task_id, task_revision
        )
        task_view = self._query_task_view(task_id, task_revision, event.version)
        snapshot = self._query_agent_snapshot(event.version)
        task = dict(task_view.get("task", {})) if task_view else {}
        role_slots = [RoleSlot.from_dict(value) for value in task.get("role_slots", [])]
        availability = self._execution_availability(role_slots)
        skill_references = self._skill_references(task, snapshot)
        local_proposal = self._local_proposal(
            event, task_view, snapshot, role_slots, availability, skill_references
        )
        evidence_view = self._query_evidence_view(task_id, event.version)
        loop_input = LoopInput(
            event=event,
            offset=envelope.offset,
            now=self.blackboard.now(),
            session=session,
            task_view=task_view,
            agent_snapshot=snapshot,
            evidence_view=evidence_view,
            execution_availability=availability,
            skill_references=skill_references,
            local_proposal=local_proposal,
        )
        result = self.loop.step(loop_input)
        if not result.consume_input:
            return False

        cleanup_task_id: Optional[str] = None
        raw_receipts_to_remove: list[tuple[str, str]] = []
        has_submit = any(
            effect.kind == EffectKind.SUBMIT_ACTION_INTENT for effect in result.effects
        )
        if has_submit:
            # proposed 必须先持久化；ActionExecutor 在物理下发前再原子推进为 dispatching。
            self.store.save_session(result.session_after)

        self._current_input_offset = envelope.offset
        self._pending_commit_keys = []
        try:
            for effect in result.effects:
                if effect.kind == EffectKind.APPEND_BLACKBOARD_EVENT:
                    self._publish_task_event(effect.payload)
                elif effect.kind == EffectKind.SUBMIT_ACTION_INTENT:
                    execution = self.action_executor.execute(effect.payload)
                    if execution.receipt_event is not None:
                        raw_receipts_to_remove.append(
                            (task_id, effect.payload.intent.intent_id)
                        )
                elif effect.kind == EffectKind.CLEANUP_TASK_SESSION:
                    cleanup_task_id = str(effect.payload)
        except Exception:
            self._pending_commit_keys = []
            raise
        finally:
            self._current_input_offset = None

        persisted = self.store.load_session(task_id) if has_submit else None
        self.store.save_session(persisted or result.session_after)
        self.store.save_consumer_offset(envelope.offset)
        for pending_task_id, key in self._pending_commit_keys:
            self.store.resolve_task_event(pending_task_id, key)
        self._pending_commit_keys = []
        for receipt_task_id, intent_id in raw_receipts_to_remove:
            self.store.remove_raw_receipt(receipt_task_id, intent_id)
        if cleanup_task_id:
            self.store.cleanup_task(cleanup_task_id)
        return True

    def _publisher_for_task(
        self, task_id: str, source_offset: Optional[int] = None
    ) -> DurableEventPublisher:
        return DurableEventPublisher(
            self.blackboard,
            lambda event: self.store.stage_task_event(
                task_id, event, source_offset=source_offset
            ),
            lambda key: self.store.resolve_task_event(task_id, key),
            lambda key, result: self.store.reject_task_event(task_id, key, result),
            auto_resolve_success=False,
        )

    def _publish_task_event(self, event: BlackboardEvent):
        task_id = str(event_content(event).get("task_id", ""))
        if not task_id:
            raise ValueError("ACTION_EVENT_TASK_ID_REQUIRED")
        publisher = self._publisher_for_task(task_id, self._current_input_offset)
        result = publisher.publish(event)
        if (
            result.status in (AppendStatus.APPENDED, AppendStatus.DUPLICATE_SAME)
            and event.idempotency_key
        ):
            self._pending_commit_keys.append((task_id, event.idempotency_key))
        return result

    def _query_task_view(
        self, task_id: str, task_revision: int, min_version: int
    ) -> dict[str, Any]:
        value = self.blackboard.query_view(
            Ledger.TASK.value,
            {
                "view_type": "task_coordination",
                "task_id": task_id,
                "task_revision": task_revision,
            },
            min_version,
            self.view_timeout_s,
        )
        return dict(value or {})

    def _query_agent_snapshot(self, min_version: int) -> AgentPublicSnapshot:
        value = self.blackboard.query_view(
            None,
            {"view_type": "agent_public", "device_id": self.spec.device_id},
            min_version,
            self.view_timeout_s,
        )
        if isinstance(value, list):
            value = value[0] if value else {}
        if isinstance(value, dict) and "device_id" not in value:
            value = value.get(self.spec.device_id, value.get("snapshot", {}))
        return AgentPublicSnapshot.from_dict(dict(value or {}))

    def _query_evidence_view(self, task_id: str, min_version: int) -> Optional[dict]:
        try:
            value = self.blackboard.query_view(
                Ledger.EVIDENCE.value,
                {"view_type": "evidence", "task_id": task_id},
                min_version,
                self.view_timeout_s,
            )
            return dict(value or {})
        except (KeyError, NotImplementedError):
            return None

    def _execution_availability(
        self, role_slots: list[RoleSlot]
    ) -> list[ExecutionAvailability]:
        now = self.blackboard.now()
        verbs = sorted(
            {
                str(slot.action_template.get("verb", ""))
                for slot in role_slots
                if slot.action_template.get("verb")
            }
        )
        return [
            ExecutionAvailability(
                device_id=self.spec.device_id,
                verb=verb,
                available=self.action_gateway.is_available(self.spec.device_id, verb),
                checked_at=now,
            )
            for verb in verbs
        ]

    def _skill_references(
        self, task: dict[str, Any], snapshot: AgentPublicSnapshot
    ) -> list:
        query = SkillQuery(
            task_type=str(task.get("task_type", "")),
            situation_tags=list(task.get("extra", {}).get("situation_tags", [])),
            capability_ids=list(snapshot.capabilities),
        )
        return self.skill_provider.search(query, limit=3)

    def _local_proposal(
        self,
        event: BlackboardEvent,
        task_view: dict[str, Any],
        snapshot: AgentPublicSnapshot,
        role_slots: list[RoleSlot],
        availability: list[ExecutionAvailability],
        skill_references: list,
    ) -> Optional[CollaborationProposal]:
        self.last_local_proposal_error = None
        if (
            not self.spec.local_proposal_enabled
            or self.local_proposal_policy is None
            or event_type_value(event) != CoordinationEventType.BID_ROUND_OPENED.value
        ):
            return None
        content = event_content(event)
        task = dict(task_view.get("task", {}))
        bid_engine = getattr(self.loop, "bid_engine", None)
        if bid_engine is None:
            return None
        hard_offers = [
            bid_engine.evaluate_slot(
                task=task,
                slot=slot,
                snapshot=snapshot,
                execution_availability=availability,
                now=self.blackboard.now(),
            )
            for slot in role_slots
        ]
        context = {
            "task": task,
            "role_slots": role_slots,
            "self_snapshot": snapshot,
            "hard_offers": hard_offers,
            "relevant_clues": [],
            "skill_references": skill_references,
            "task_revision": int(content.get("task_revision", 0)),
            "coordination_epoch": int(content.get("coordination_epoch", 0)),
        }
        context["context_fingerprint"] = fingerprint(context)
        try:
            proposal = self.local_proposal_policy.propose(
                to_json_value(context), self.spec.local_proposal_timeout_s
            )
        except Exception as exc:
            # Local model advice is optional and must never stop a hard BID.
            # Keep one sanitized diagnostic for runtime observability; client
            # exceptions already redact credentials and unsafe response bodies.
            self.last_local_proposal_error = f"{type(exc).__name__}: {exc}"
            return None
        barrier = self.blackboard.high_watermark()
        refreshed = self._query_task_view(
            str(content.get("task_id", "")),
            int(content.get("task_revision", 0)),
            barrier,
        )
        if refreshed.get("terminal_event_id") is not None:
            return None
        if int(refreshed.get("task_revision", 0)) != int(
            content.get("task_revision", 0)
        ):
            return None
        if int(refreshed.get("coordination_epoch", 0)) != int(
            content.get("coordination_epoch", 0)
        ):
            return None
        active_round = refreshed.get("active_bid_round") or {}
        if active_round and int(active_round.get("bid_round", 0)) != int(
            content.get("bid_round", 1)
        ):
            return None
        return proposal

    def _mark_dispatching(self, payload) -> None:
        task_id = payload.intent.task_id or ""
        session = self.store.load_session(task_id)
        if session is None or session.current_intent is None:
            raise RuntimeError("INTENT_SESSION_NOT_PERSISTED")
        if session.current_intent.intent_id != payload.intent.intent_id:
            raise RuntimeError("INTENT_SESSION_MISMATCH")
        session.current_intent.state = IntentState.DISPATCHING
        session.session_version += 1
        self.store.save_session(session)


class AgentProcessSupervisor:
    """One active child process per device_id; no automatic restart in this version."""

    def __init__(self, *, start_method: str = "spawn") -> None:
        self._context = multiprocessing.get_context(start_method)
        self._processes: dict[str, multiprocessing.Process] = {}
        self._statuses: dict[str, ProcessStatus] = {}

    def start(
        self,
        spec: AgentProcessSpec,
        target: Callable[..., None],
        args: tuple = (),
    ) -> ProcessStatus:
        current = self._processes.get(spec.device_id)
        if current is not None and current.is_alive():
            raise ValueError(f"device process already active: {spec.device_id}")
        process = self._context.Process(
            target=target,
            args=(spec, *args),
            name=f"agent-{spec.device_id}",
            daemon=False,
        )
        status = ProcessStatus(
            device_id=spec.device_id,
            state=ProcessState.STARTING,
            pid=None,
            last_exit_code=None,
            reason_code=None,
            updated_at=time.time(),
        )
        self._statuses[spec.device_id] = status
        process.start()
        self._processes[spec.device_id] = process
        status.pid = process.pid
        status.state = ProcessState.READY
        status.updated_at = time.time()
        return status

    def status(self, device_id: str) -> Optional[ProcessStatus]:
        process = self._processes.get(device_id)
        status = self._statuses.get(device_id)
        if process is None or status is None:
            return None
        if not process.is_alive() and status.state != ProcessState.STOPPED:
            status.state = ProcessState.STOPPED
            status.last_exit_code = process.exitcode
            status.reason_code = "PROCESS_EXITED" if process.exitcode else None
            status.updated_at = time.time()
        return status

    def stop(self, device_id: str, timeout_s: float = 5.0) -> Optional[ProcessStatus]:
        process = self._processes.get(device_id)
        if process is None:
            return self._statuses.get(device_id)
        if process.is_alive():
            process.terminate()
            process.join(timeout_s)
        status = self._statuses[device_id]
        status.state = ProcessState.STOPPED
        status.last_exit_code = process.exitcode
        status.updated_at = time.time()
        return status


def _normalize_envelope(value: Any) -> EventEnvelope:
    if isinstance(value, EventEnvelope):
        return value
    if isinstance(value, dict):
        return EventEnvelope(offset=int(value["offset"]), event=value["event"])
    return EventEnvelope(offset=int(value.offset), event=value.event)
