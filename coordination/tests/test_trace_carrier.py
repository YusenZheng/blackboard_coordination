from __future__ import annotations

import tempfile
import unittest

from swarm_brain.blackboard.board import Blackboard
from swarm_brain.contracts.blackboard_event import AppendStatus, Ledger
from swarm_brain.coordination.file_session_store import FileTaskSessionStore
from swarm_brain.coordination.models import (
    CoordinationEventType,
    make_blackboard_event,
)


def _task_payload() -> dict:
    return {
        "schema_version": 2,
        "task_id": "task-trace-carrier",
        "task_revision": 1,
        "task_type": "search",
        "goal": "search target",
        "requirement": {
            "required_capabilities": ["search"],
            "min_battery": 0.2,
            "min_endurance_s": 60,
            "space_constraints": [],
        },
        "role_slots": [
            {
                "slot_id": "searcher",
                "required_capability_ids": ["search"],
                "exclusive": True,
                "allowed_actions": ["move_to"],
                "action_template": {
                    "verb": "move_to",
                    "params": {"target": "zone-a"},
                    "reversible": True,
                },
                "completion_rule": {
                    "kind": "receipt_success",
                    "required_result_fields": [],
                },
            }
        ],
    }


class TraceCarrierContractTest(unittest.TestCase):
    def test_trace_carrier_does_not_change_blackboard_business_identity(self) -> None:
        board = Blackboard(clock=lambda: 100.0)
        key = "task-posted:task-trace-carrier:1"
        first_carrier = {
            "traceparent": "00-11111111111111111111111111111111-1111111111111111-01",
            "tracestate": "vendor=first",
        }
        retry_carrier = {
            "traceparent": "00-22222222222222222222222222222222-2222222222222222-01",
            "tracestate": "vendor=retry",
        }
        first = make_blackboard_event(
            CoordinationEventType.TASK_POSTED,
            Ledger.TASK,
            _task_payload(),
            "ingress",
            key,
            trace_carrier=first_carrier,
        )
        retry = make_blackboard_event(
            CoordinationEventType.TASK_POSTED,
            Ledger.TASK,
            _task_payload(),
            "ingress",
            key,
            trace_carrier=retry_carrier,
        )

        appended = board.append(first)
        duplicate = board.append(retry)

        self.assertEqual(AppendStatus.APPENDED, appended.status)
        self.assertEqual(AppendStatus.DUPLICATE_SAME, duplicate.status)
        self.assertEqual(appended.offset, duplicate.offset)
        self.assertEqual(appended.version, duplicate.version)
        self.assertEqual(1, board.high_watermark())
        committed = board.read_since(-1, None, 10, 0)[0].event
        self.assertEqual(first_carrier, committed.trace_carrier)

    def test_trace_carrier_roundtrips_through_task_outbox(self) -> None:
        first_carrier = {
            "traceparent": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
            "tracestate": "swarm=dog-a",
        }
        retry_carrier = {
            "traceparent": "00-cccccccccccccccccccccccccccccccc-dddddddddddddddd-01",
        }
        key = "bid:task-trace-carrier:1:1:1:dog-a"
        first = make_blackboard_event(
            CoordinationEventType.BID,
            Ledger.TASK,
            {"task_id": "task-trace-carrier", "device_id": "dog-a"},
            "dog-a",
            key,
            trace_carrier=first_carrier,
        )
        regenerated_after_restart = make_blackboard_event(
            CoordinationEventType.BID,
            Ledger.TASK,
            {"task_id": "task-trace-carrier", "device_id": "dog-a"},
            "dog-a",
            key,
            trace_carrier=retry_carrier,
        )

        with tempfile.TemporaryDirectory() as root:
            store = FileTaskSessionStore(root, "dog-a")
            staged = store.stage_task_event(
                "task-trace-carrier", first, source_offset=7
            )
            replayed = store.stage_task_event(
                "task-trace-carrier",
                regenerated_after_restart,
                source_offset=99,
            )
            records = store.list_task_event_records("task-trace-carrier")
            # Remove the long SHA-256 outbox filename through the store's
            # Windows extended-path helper before TemporaryDirectory cleanup.
            store.resolve_task_event("task-trace-carrier", key)

        self.assertEqual(first_carrier, staged.trace_carrier)
        self.assertEqual(first_carrier, replayed.trace_carrier)
        self.assertEqual(1, len(records))
        self.assertEqual(first_carrier, records[0].event.trace_carrier)
        self.assertEqual(7, records[0].source_offset)


if __name__ == "__main__":
    unittest.main()
