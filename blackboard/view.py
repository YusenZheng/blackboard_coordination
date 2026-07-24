# STATUS: STAGED(A类)—— v1 通用视图 + coordination v2 权威投影视图
"""从 append-only 事件流折叠当前状态。

``view_version`` 始终表示投影已处理的全局事件水位，而不是某个实体最后一次
变化的版本。内存实现同步折叠，所以它与 Blackboard.high_watermark() 相同。
"""
from __future__ import annotations

import copy
from enum import Enum
from typing import Any, Optional

from ..contracts.blackboard_event import BlackboardEvent, Ledger


def _value(value: Any) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def _event_type(event: BlackboardEvent) -> str:
    return _value(event.type)


def _ledger_value(value: Any) -> Optional[str]:
    return None if value is None else _value(value)


def _revision(content: dict[str, Any], default: int = 1) -> int:
    try:
        return int(content.get("task_revision", default))
    except (TypeError, ValueError):
        return default


def _metadata_record(event: BlackboardEvent) -> dict[str, Any]:
    record = copy.deepcopy(event.content)
    record.update(
        {
            "event_id": event.id,
            "event_version": int(event.version),
            "event_ts": float(event.ts),
        }
    )
    return record


def _matches_task(
    event: BlackboardEvent,
    task_id: str,
    task_revision: Optional[int] = None,
    *,
    allow_missing_revision: bool = False,
) -> bool:
    content = event.content
    if str(content.get("task_id", "")) != task_id:
        return False
    if task_revision is None:
        return True
    if allow_missing_revision and "task_revision" not in content:
        return True
    return _revision(content) == task_revision


def _latest_task_revision(events: list[BlackboardEvent], task_id: str) -> int:
    revisions = [
        _revision(event.content)
        for event in events
        if _event_type(event) == "task_posted"
        and str(event.content.get("task_id", "")) == task_id
    ]
    return max(revisions, default=1)


def _effective_task_revisions(events: list[BlackboardEvent]) -> list[int]:
    """Resolve legacy events without revision at their position in the stream.

    A legacy CLUE belongs to the newest revision that had already been posted
    when the clue was committed.  Looking at the final stream-wide maximum
    would retroactively move old evidence whenever a new revision is posted.
    """

    latest_posted: dict[str, int] = {}
    result: list[int] = []
    for event in events:
        task_id = str(event.content.get("task_id", ""))
        if _event_type(event) == "task_posted" and task_id:
            latest_posted[task_id] = max(
                latest_posted.get(task_id, 1), _revision(event.content)
            )
        if "task_revision" in event.content:
            result.append(_revision(event.content))
        else:
            result.append(latest_posted.get(task_id, 1))
    return result


def fold_view(
    events: list[BlackboardEvent],
    ledger: Optional[Ledger | str] = None,
    filt: Optional[dict] = None,
    *,
    view_version: Optional[int] = None,
    now: float = 0.0,
    agent_snapshots: Optional[dict[str, dict[str, Any]]] = None,
) -> dict | list[dict]:
    """折叠事件。

    没有 ``view_type`` 时返回旧 skeleton 使用的四账本总览；有 ``view_type``
    时返回 coordination v2 的窄视图。
    """

    watermark = len(events) if view_version is None else int(view_version)
    query = dict(filt or {})
    view_type = query.get("view_type")
    if view_type == "task_coordination":
        return _task_coordination_view(events, query, watermark)
    if view_type == "bid_round":
        return _bid_round_view(events, query, watermark)
    if view_type == "agent_public":
        return _agent_public_view(
            events, query, watermark, agent_snapshots or {}, now
        )
    if view_type == "evidence":
        return _evidence_view(events, query, watermark, now)
    if view_type == "action":
        return _action_view(events, query, watermark)
    if view_type == "terminal":
        return _terminal_view(events, query, watermark)
    if view_type is not None:
        raise KeyError(f"unknown Blackboard view_type: {view_type}")
    return _legacy_view(events, ledger, query, watermark)


def _task_coordination_view(
    events: list[BlackboardEvent], query: dict[str, Any], watermark: int
) -> dict[str, Any]:
    task_id = str(query.get("task_id", ""))
    task_revision = int(query.get("task_revision", 1))
    view: dict[str, Any] = {
        "task_id": task_id,
        "task_revision": task_revision,
        "task": {},
        "status": "unknown",
        "coordination_epoch": 0,
        "active_bid_round": None,
        "current_plan": None,
        "completed_assignment_ids": [],
        "terminal_event_id": None,
        "terminal_event_type": None,
        "terminal_content": None,
        "terminal_event_version": None,
        "terminal_event_ts": None,
        "pending_replan_cause_ids": [],
        "replan_pending": False,
        "view_version": watermark,
    }
    pending: list[str] = []
    completed: list[str] = []
    effective_revisions = _effective_task_revisions(events)

    for event_index, event in enumerate(events):
        event_type = _event_type(event)
        if event_type == "clue" and "task_revision" not in event.content:
            matches = (
                str(event.content.get("task_id", "")) == task_id
                and effective_revisions[event_index] == task_revision
            )
        else:
            matches = _matches_task(event, task_id, task_revision)
        if not matches:
            continue
        content = event.content

        # 第一个终态冻结任务；后续业务事件只能推进全局 view_version。
        if view["terminal_event_id"] is not None:
            continue

        if event_type == "task_posted":
            if not view["task"]:
                view["task"] = copy.deepcopy(content)
                view["status"] = "posted"
        elif event_type == "bid_round_opened":
            epoch = int(content.get("coordination_epoch", 0))
            round_number = int(content.get("bid_round", 1))
            active = view.get("active_bid_round") or {}
            active_round = int(active.get("bid_round", 0))
            current_epoch = int(view.get("coordination_epoch", 0))
            expected_epoch = 1 if current_epoch == 0 else current_epoch
            if (
                epoch == expected_epoch
                and not view.get("current_plan")
                and round_number > active_round
            ):
                view["coordination_epoch"] = epoch
                view["active_bid_round"] = copy.deepcopy(content)
                view["status"] = "bidding"
        elif event_type == "task_assigned":
            epoch = int(content.get("coordination_epoch", 0))
            if epoch != int(view.get("coordination_epoch", 0)):
                continue
            if content.get("schema_version") == 2 and not view.get(
                "active_bid_round"
            ):
                continue
            previous_plan = view.get("current_plan") or {}
            if (
                previous_plan
                and epoch == int(view.get("coordination_epoch", 0))
                and previous_plan.get("plan_id") != content.get("plan_id")
            ):
                # A committed epoch has one canonical plan. A different plan in the
                # same epoch is stale/corrupt input and must not steal ownership.
                continue
            if previous_plan.get("plan_id") != content.get("plan_id"):
                completed = []
            view["coordination_epoch"] = epoch
            view["current_plan"] = copy.deepcopy(content)
            view["active_bid_round"] = None
            view["status"] = "assigned"
            consumed = {str(item) for item in content.get("input_evidence_refs", [])}
            pending = [item for item in pending if item not in consumed]
        elif event_type == "action_intent":
            plan = view.get("current_plan") or {}
            if (
                content.get("plan_id") == plan.get("plan_id")
                and int(content.get("coordination_epoch", -1))
                == int(view.get("coordination_epoch", -2))
            ):
                view["status"] = "running"
        elif event_type == "assignment_completed":
            plan = view.get("current_plan")
            if not isinstance(plan, dict):
                continue
            if (
                content.get("plan_id") != plan.get("plan_id")
                or int(content.get("coordination_epoch", -1))
                != int(plan.get("coordination_epoch", -2))
            ):
                continue
            assignment_id = str(content.get("assignment_id", ""))
            assignment = next(
                (
                    item
                    for item in plan.get("assignments", [])
                    if str(item.get("assignment_id", "")) == assignment_id
                ),
                None,
            )
            if assignment is None or int(content.get("assignment_epoch", -1)) != int(
                assignment.get("assignment_epoch", -2)
            ):
                continue
            if assignment_id and assignment_id not in completed:
                completed.append(assignment_id)
        elif event_type == "clue":
            if event.id not in pending:
                pending.append(event.id)
        elif event_type == "task_replan":
            from_epoch = int(content.get("from_epoch", -1))
            to_epoch = int(content.get("to_epoch", -1))
            if (
                from_epoch != int(view.get("coordination_epoch", 0))
                or to_epoch != from_epoch + 1
            ):
                continue
            consumed = {
                str(item) for item in content.get("evidence_refs", [])
            }
            cause = content.get("cause_event_id")
            if cause:
                consumed.add(str(cause))
            if cause not in pending or not consumed.issubset(set(pending)):
                continue
            pending = [item for item in pending if item not in consumed]
            view["coordination_epoch"] = to_epoch
            view["active_bid_round"] = None
            view["current_plan"] = None
            completed = []
            view["status"] = "replanning"
        elif event_type in ("task_done", "task_failed"):
            if content.get("schema_version") == 2:
                if int(content.get("coordination_epoch", -1)) != int(
                    view.get("coordination_epoch", 0)
                ):
                    continue
                if event_type == "task_done":
                    plan = view.get("current_plan") or {}
                    expected = {
                        str(item.get("assignment_id", ""))
                        for item in plan.get("assignments", [])
                        if item.get("assignment_id")
                    }
                    if (
                        not plan
                        or content.get("plan_id") != plan.get("plan_id")
                        or not expected
                        or not expected.issubset(set(completed))
                    ):
                        continue
            view["terminal_event_id"] = event.id
            view["terminal_event_type"] = event_type
            view["terminal_content"] = copy.deepcopy(content)
            view["terminal_event_version"] = int(event.version)
            view["terminal_event_ts"] = float(event.ts)
            view["active_bid_round"] = None
            view["status"] = "done" if event_type == "task_done" else "failed"
            pending = []

    view["completed_assignment_ids"] = sorted(completed)
    view["pending_replan_cause_ids"] = list(pending)
    view["replan_pending"] = bool(pending) and view["terminal_event_id"] is None
    return view


def _bid_round_view(
    events: list[BlackboardEvent], query: dict[str, Any], watermark: int
) -> dict[str, Any]:
    task_id = str(query.get("task_id", ""))
    revision = int(query.get("task_revision", 1))
    epoch = int(query.get("coordination_epoch", 0))
    bid_round = int(query.get("bid_round", 1))
    bids: dict[str, dict[str, Any]] = {}
    opened: Optional[dict[str, Any]] = None

    for event in events:
        if not _matches_task(event, task_id, revision):
            continue
        content = event.content
        if (
            int(content.get("coordination_epoch", -1)) != epoch
            or int(content.get("bid_round", 1)) != bid_round
        ):
            continue
        event_type = _event_type(event)
        if event_type == "bid_round_opened":
            if opened is None:
                opened = _metadata_record(event)
        elif event_type == "bid":
            if opened is None:
                continue
            deadline = min(
                float(opened.get("deadline", float("inf"))),
                float(content.get("expires_at", float("inf"))),
            )
            if float(event.ts) > deadline:
                continue
            device_id = str(content.get("device_id", ""))
            if not device_id:
                continue
            bids[device_id] = {
                "event_id": event.id,
                "event_version": int(event.version),
                "event_ts": float(event.ts),
                "payload": copy.deepcopy(content),
            }

    return {
        "task_id": task_id,
        "task_revision": revision,
        "coordination_epoch": epoch,
        "bid_round": bid_round,
        "round_opened": opened,
        "bids_by_device": {key: bids[key] for key in sorted(bids)},
        "view_version": watermark,
    }


def _evidence_view(
    events: list[BlackboardEvent], query: dict[str, Any], watermark: int, now: float
) -> dict[str, Any]:
    task_id = str(query.get("task_id", ""))
    requested_revision = int(
        query.get("task_revision", _latest_task_revision(events, task_id))
    )
    clues: dict[str, dict[str, Any]] = {}
    validity: dict[str, bool] = {}
    effective_revisions = _effective_task_revisions(events)

    for event_index, event in enumerate(events):
        if _event_type(event) != "clue":
            continue
        if str(event.content.get("task_id", "")) != task_id:
            continue
        if effective_revisions[event_index] != requested_revision:
            continue
        content = event.content
        clue_key = str(content.get("clue_id") or content.get("dedupe_key") or event.id)
        record = _metadata_record(event)
        record["payload"] = copy.deepcopy(content)
        record["confidence"] = event.confidence
        record["ttl"] = event.ttl
        clues[clue_key] = record
        expires_at = content.get("expires_at")
        valid = True
        if event.ttl is not None and event.ts + float(event.ttl) < now:
            valid = False
        if expires_at is not None and float(expires_at) < now:
            valid = False
        validity[clue_key] = valid

    ordered = sorted(
        (key for key in clues if validity.get(key, True)),
        key=lambda key: (int(clues[key]["event_version"]), key),
    )
    return {
        "task_id": task_id,
        "task_revision": requested_revision,
        "clues_by_key": {key: clues[key] for key in sorted(clues)},
        "valid_clue_keys": ordered,
        "view_version": watermark,
    }


def _action_view(
    events: list[BlackboardEvent], query: dict[str, Any], watermark: int
) -> dict[str, Any]:
    task_id = str(query.get("task_id", ""))
    revision = int(query.get("task_revision", 1))
    intents: dict[str, dict[str, Any]] = {}
    receipts: dict[str, dict[str, Any]] = {}
    intercepts: dict[str, list[dict[str, Any]]] = {}

    for event in events:
        if not _matches_task(event, task_id, revision):
            continue
        event_type = _event_type(event)
        if event_type not in ("action_intent", "receipt", "safety_intercept"):
            continue
        intent_id = str(event.content.get("intent_id", ""))
        if not intent_id:
            continue
        record = _metadata_record(event)
        if event_type == "action_intent":
            intents[intent_id] = record
        elif event_type == "receipt":
            receipts[intent_id] = record
        else:
            intercepts.setdefault(intent_id, []).append(record)

    return {
        "task_id": task_id,
        "task_revision": revision,
        "intents_by_id": {key: intents[key] for key in sorted(intents)},
        "receipts_by_intent": {key: receipts[key] for key in sorted(receipts)},
        "intercepts_by_intent": {
            key: sorted(intercepts[key], key=lambda item: item["event_version"])
            for key in sorted(intercepts)
        },
        "view_version": watermark,
    }


def _terminal_view(
    events: list[BlackboardEvent], query: dict[str, Any], watermark: int
) -> dict[str, Any]:
    task_id = str(query.get("task_id", ""))
    revision = int(query.get("task_revision", 1))
    task_view = _task_coordination_view(
        events,
        {"task_id": task_id, "task_revision": revision},
        watermark,
    )
    event_id = task_view.get("terminal_event_id")
    event_type = task_view.get("terminal_event_type")
    content = task_view.get("terminal_content")
    return {
        "task_id": task_id,
        "task_revision": revision,
        "terminal_event_id": event_id,
        "terminal_event_type": event_type,
        "status": (
            "done" if event_type == "task_done" else "failed" if event_type else None
        ),
        "terminal_event": (
            {
                "event_id": event_id,
                "event_version": int(task_view["terminal_event_version"]),
                "event_ts": float(task_view["terminal_event_ts"]),
                "payload": copy.deepcopy(content),
            }
            if event_id is not None
            else None
        ),
        "view_version": watermark,
    }


def _agent_public_view(
    events: list[BlackboardEvent],
    query: dict[str, Any],
    watermark: int,
    snapshots: dict[str, dict[str, Any]],
    now: float,
) -> dict | list[dict]:
    busy_by_device = _active_assignment_map(events, watermark)

    def project(device_id: str) -> Optional[dict[str, Any]]:
        source = snapshots.get(device_id)
        if source is None:
            return None
        value = copy.deepcopy(source)
        value["device_id"] = device_id
        assigned_task = busy_by_device.get(device_id)
        if assigned_task:
            value["busy"] = True
            value["busy_task_id"] = assigned_task
        else:
            value.setdefault("busy", False)
            value.setdefault("busy_task_id", None)
        value.setdefault("state_updated_at", now)
        value.setdefault("card_version", 1)
        value["view_version"] = watermark
        return value

    if query.get("device_id") is not None:
        device_id = str(query["device_id"])
        value = project(device_id)
        return value if value is not None else {
            "device_id": device_id,
            "online": False,
            "healthy": False,
            "view_version": watermark,
        }

    requested = query.get("device_ids")
    device_ids = (
        [str(item) for item in requested]
        if isinstance(requested, (list, tuple, set))
        else sorted(snapshots)
    )
    result: dict[str, Any] = {"view_version": watermark}
    for device_id in device_ids:
        value = project(device_id)
        if value is not None:
            result[device_id] = value
    return result


def _active_assignment_map(
    events: list[BlackboardEvent], watermark: int
) -> dict[str, str]:
    # A newer task revision supersedes the ownership projection of every older
    # revision.  Folding all historical revisions would keep an agent busy on
    # rev1 even after rev2 has been posted and is trying to form a new team.
    latest_revisions: dict[str, int] = {}
    for event in events:
        if _event_type(event) != "task_posted":
            continue
        task_id = str(event.content.get("task_id", ""))
        if task_id:
            latest_revisions[task_id] = max(
                latest_revisions.get(task_id, 1), _revision(event.content)
            )
    busy: dict[str, str] = {}
    for task_id, revision in sorted(latest_revisions.items()):
        task_view = _task_coordination_view(
            events,
            {"task_id": task_id, "task_revision": revision},
            watermark,
        )
        if task_view.get("terminal_event_id") is not None:
            continue
        plan = task_view.get("current_plan") or {}
        for assignment in plan.get("assignments", []):
            device_id = str(assignment.get("device_id", ""))
            if device_id:
                busy.setdefault(device_id, task_id)
    return busy


def _legacy_view(
    events: list[BlackboardEvent],
    ledger: Optional[Ledger | str],
    query: dict[str, Any],
    watermark: int,
) -> dict[str, Any]:
    """兼容 v1 Harness 的四账本总览。"""

    tasks: dict[str, dict[str, Any]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    receipts: dict[str, dict[str, Any]] = {}
    human: dict[str, dict[str, Any]] = {}
    requested_ledger = _ledger_value(ledger)
    task_filter = query.get("task_id")

    for event in events:
        if requested_ledger is not None and _ledger_value(event.ledger) != requested_ledger:
            continue
        content = event.content
        if task_filter is not None and content.get("task_id") != task_filter:
            continue
        event_type = _event_type(event)
        task_id = str(content.get("task_id", ""))
        if event_type == "task_posted" and task_id:
            tasks[task_id] = {
                "status": "posted",
                "owner": None,
                "content": copy.deepcopy(content),
            }
        elif event_type == "bid_round_opened" and task_id:
            tasks.setdefault(task_id, {})["status"] = "bidding"
        elif event_type == "task_assigned" and task_id:
            task = tasks.setdefault(task_id, {})
            assignments = list(content.get("assignments", []))
            owners = [item.get("device_id") for item in assignments]
            legacy_owner = content.get("device_id")
            task["status"] = "assigned"
            task["owner"] = legacy_owner or (owners[0] if len(owners) == 1 else None)
            task["owners"] = owners
            task["plan"] = copy.deepcopy(content)
        elif event_type == "task_replan" and task_id:
            tasks.setdefault(task_id, {})["status"] = "replanning"
        elif event_type in ("task_done", "task_failed") and task_id:
            tasks.setdefault(task_id, {})["status"] = (
                "done" if event_type == "task_done" else "failed"
            )
        elif event_type == "clue":
            key = str(content.get("clue_id") or event.id)
            evidence[key] = copy.deepcopy(content)
        elif event_type == "receipt":
            key = str(content.get("intent_id") or event.id)
            receipts[key] = copy.deepcopy(content)
        elif event_type in ("auth_point", "auth_decision"):
            key = str(content.get("intent_id") or event.id)
            human.setdefault(key, {})[event_type] = copy.deepcopy(content)

    return {
        "tasks": tasks,
        "evidence": evidence,
        "receipts": receipts,
        "human": human,
        "view_version": watermark,
    }
