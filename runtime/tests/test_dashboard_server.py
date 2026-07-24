from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from swarm_brain.runtime.dashboard_server import (
    DashboardHTTPServer,
    LiveRun,
    RunRegistry,
    _parse_candidate_decision_path,
    _parse_run_path,
)
from swarm_brain.observability import ObservabilityHub


class FakeRuntime:
    def run(
        self,
        instruction,
        *,
        event_listener,
        status_listener,
        llm_listener,
        session_listener,
    ):
        status_listener({"stage": "starting", "message": "开始"})
        llm_listener(
            {
                "operation": "intent_interpretation",
                "model": "deepseek-v4-flash",
                "status": "ok",
                "duration_ms": 120.0,
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
            }
        )
        session_listener(
            {
                "device_id": "dog-a",
                "phase": "after_bid",
                "exists": True,
                "session": {"state": "standby"},
            }
        )
        event_listener(
            {
                "offset": 0,
                "version": 1,
                "type": "task_posted",
                "source": "ingress",
                "event_id": "event-1",
                "ts": 100.0,
                "content": {"task_id": "task-1", "goal": instruction},
            }
        )
        status_listener({"stage": "done", "message": "完成"})
        return {"status": "ok"}


class FakeObservabilityStore:
    def __init__(self) -> None:
        self.stream: dict[str, list[dict]] = {}

    def list_runs(self, *, limit, offset):
        return [
            {
                "run_id": "persisted-run",
                "instruction": "历史任务",
                "status": "completed",
                "started_at": 1.0,
            }
        ][offset : offset + limit]

    def get_run(self, run_id):
        if run_id != "persisted-run":
            return None
        return {
            "run_id": run_id,
            "instruction": "历史任务",
            "status": "completed",
            "result": {"status": "ok"},
        }

    def get_timeline(self, run_id, *, since, limit):
        return {
            "events": [
                {
                    "seq": 1,
                    "kind": "blackboard_event",
                    "data": {"type": "task_posted", "content": {}},
                }
            ][since:limit]
        }

    def get_trace(self, run_id):
        return {
            "spans": [
                {
                    "span_id": "root",
                    "parent_span_id": "",
                    "name": "swarm_brain.chain1.run",
                }
            ],
            "links": [],
        }

    def get_logs(self, run_id, *, level=None, search=None, limit=500):
        return [{"level": "INFO", "message": "ready"}]

    def get_errors(self, run_id, *, limit=500):
        return [{"level": "ERROR", "message": "expected test error"}]

    def get_metrics(self, run_id):
        return [{"name": "run.total", "value": 1}]

    def get_payload(self, run_id, payload_id):
        if payload_id != "payload-1":
            return None
        return {"payload_id": payload_id, "run_id": run_id, "summary": "safe"}

    def append_stream_event(self, run_id, message):
        self.stream.setdefault(run_id, []).append(dict(message))

    def get_stream_events(self, run_id, *, since):
        return [
            row for row in self.stream.get(run_id, []) if row["seq"] > since
        ]

    def health(self):
        return {"status": "ok", "last_successful_write": 123.0}


class FakeMemoryStore:
    def __init__(self) -> None:
        self.candidates = {
            "candidate-1": {
                "id": "candidate-1",
                "status": "pending_review",
                "candidate_type": "fact",
            }
        }

    def list_candidates(self, status="pending_review", limit=100):
        return [
            row
            for row in self.candidates.values()
            if status == "all" or row["status"] == status
        ][:limit]

    def decide_candidate(
        self, candidate_id, decision, decided_by="dashboard", reason=""
    ):
        row = self.candidates[candidate_id]
        row["status"] = decision
        row["decided_by"] = decided_by
        row["reason"] = reason
        return row

    def health(self):
        return {"status": "ok"}


class DashboardServerTest(unittest.TestCase):
    def test_live_run_replays_messages_and_finishes(self) -> None:
        state = LiveRun("run-1", "测试")
        state.publish("status", {"stage": "starting"})
        values, done = state.wait_after(0, 0)
        self.assertFalse(done)
        self.assertEqual(1, values[0]["seq"])
        state.finish(result={"status": "ok"})
        self.assertTrue(state.snapshot()["done"])

    def test_registry_streams_runtime_callbacks(self) -> None:
        registry = RunRegistry(runtime_factory=FakeRuntime)
        state = registry.start("帮我找狗")
        for _ in range(100):
            if state.snapshot()["done"]:
                break
            threading.Event().wait(0.01)
        snapshot = state.snapshot()
        self.assertTrue(snapshot["done"])
        self.assertIsNone(snapshot["error"])
        messages, done = state.wait_after(0, 0)
        self.assertTrue(done)
        self.assertEqual(
            [
                "status",
                "llm_call",
                "agent_session",
                "blackboard_event",
                "status",
                "result",
            ],
            [item["kind"] for item in messages],
        )

    def test_dashboard_asset_and_api_path_parser(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "dashboard.html"
        ).read_text(encoding="utf-8")
        self.assertIn("/api/runs", html)
        self.assertIn("系统已确认事实", html)
        self.assertIn("任务内临时状态", html)
        self.assertIn("技术诊断", html)
        self.assertIn("结果未采集", html)
        self.assertIn("renderHistoricalLlmCalls", html)
        self.assertIn('"receipt.success"', html)
        self.assertIn('"blackboard.event.committed"', html)
        self.assertIn('message.kind === "agent_session"', html)
        self.assertEqual(("abc", "events"), _parse_run_path("/api/runs/abc/events"))
        self.assertEqual(("abc", None), _parse_run_path("/api/runs/abc"))
        self.assertEqual(
            ("abc", "timeline"), _parse_run_path("/api/runs/abc/timeline")
        )
        self.assertEqual(
            ("abc", "payloads/p1"),
            _parse_run_path("/api/runs/abc/payloads/p1"),
        )
        self.assertEqual(
            "candidate-1",
            _parse_candidate_decision_path(
                "/api/memory/candidates/candidate-1/decision"
            ),
        )

    def test_registry_merges_live_and_persisted_queries(self) -> None:
        observability = FakeObservabilityStore()
        registry = RunRegistry(
            runtime_factory=FakeRuntime,
            observability_store=observability,
            memory_store=FakeMemoryStore(),
        )
        state = registry.start("实时任务")
        for _ in range(100):
            if state.snapshot()["done"]:
                break
            threading.Event().wait(0.01)
        runs = registry.list_runs()
        self.assertEqual(2, runs["count"])
        self.assertFalse(runs["degraded"])
        self.assertEqual(
            "swarm_brain.chain1.run",
            registry.trace("persisted-run")["spans"][0]["name"],
        )
        self.assertEqual(
            "safe",
            registry.payload("persisted-run", "payload-1")["summary"],
        )
        persisted = registry.stream_events_since(state.run_id, 0)
        self.assertEqual(
            [1, 2, 3, 4, 5, 6],
            [row["seq"] for row in persisted],
        )

    def test_server_rejects_non_loopback_binding(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            DashboardHTTPServer(("0.0.0.0", 0))

    def test_http_server_serves_dashboard_and_health(self) -> None:
        server = DashboardHTTPServer(
            ("127.0.0.1", 0), registry=RunRegistry(runtime_factory=FakeRuntime)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            with urlopen(f"http://{host}:{port}/", timeout=2) as response:
                html = response.read().decode("utf-8")
            with urlopen(f"http://{host}:{port}/api/health", timeout=2) as response:
                health = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertIn("V2 协同可观测平台", html)
        self.assertIn('"status": "ok"', health)

    def test_historical_observability_and_memory_http_api(self) -> None:
        registry = RunRegistry(
            runtime_factory=FakeRuntime,
            observability_store=FakeObservabilityStore(),
            memory_store=FakeMemoryStore(),
        )
        server = DashboardHTTPServer(("127.0.0.1", 0), registry=registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base = f"http://{host}:{port}"

        def get_json(path):
            with urlopen(base + path, timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            self.assertEqual(1, get_json("/api/runs")["count"])
            self.assertEqual(
                "persisted-run",
                get_json("/api/runs/persisted-run")["run_id"],
            )
            self.assertEqual(
                "task_posted",
                get_json("/api/runs/persisted-run/timeline")["events"][0][
                    "data"
                ]["type"],
            )
            self.assertEqual(
                "root",
                get_json("/api/runs/persisted-run/trace")["spans"][0][
                    "span_id"
                ],
            )
            self.assertEqual(
                "ready",
                get_json("/api/runs/persisted-run/logs")["logs"][0][
                    "message"
                ],
            )
            self.assertEqual(
                "expected test error",
                get_json("/api/runs/persisted-run/errors")["errors"][0][
                    "message"
                ],
            )
            self.assertEqual(
                "run.total",
                get_json("/api/runs/persisted-run/metrics")["metrics"][0][
                    "name"
                ],
            )
            self.assertEqual(
                "safe",
                get_json(
                    "/api/runs/persisted-run/payloads/payload-1"
                )["summary"],
            )
            self.assertEqual(
                "candidate-1",
                get_json("/api/memory/candidates")["candidates"][0]["id"],
            )
            request = Request(
                base
                + "/api/memory/candidates/candidate-1/decision",
                data=json.dumps(
                    {"decision": "approved", "notes": "verified"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=2) as response:
                decision = json.loads(response.read().decode("utf-8"))
            self.assertEqual("approved", decision["candidate"]["status"])
            self.assertEqual("verified", decision["candidate"]["reason"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_sse_emits_ids_and_resumes_after_since(self) -> None:
        registry = RunRegistry(
            runtime_factory=FakeRuntime,
            observability_store=FakeObservabilityStore(),
        )
        state = registry.start("测试 SSE")
        for _ in range(100):
            if state.snapshot()["done"]:
                break
            threading.Event().wait(0.01)
        server = DashboardHTTPServer(("127.0.0.1", 0), registry=registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            with urlopen(
                f"http://{host}:{port}/api/runs/{state.run_id}/events?since=4",
                timeout=2,
            ) as response:
                stream = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertNotIn("id: 4\n", stream)
        self.assertIn("id: 5\n", stream)
        self.assertIn("id: 6\n", stream)

    def test_registry_reads_real_sqlite_observation_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hub = ObservabilityHub(
                Path(directory) / "observability.sqlite3",
                capture_mode="metadata",
            )
            try:
                hub.start_run("sqlite-run")
                hub.event(
                    "dashboard.stream.status",
                    {
                        "category": "dashboard_stream",
                        "dashboard_seq": 1,
                    },
                    {"stage": "starting", "message": "开始"},
                )
                hub.end_run("sqlite-run", status="ok")
                self.assertTrue(hub.flush(2.0))
                registry = RunRegistry(observability_store=hub)
                self.assertEqual(
                    "sqlite-run", registry.list_runs()["runs"][0]["run_id"]
                )
                self.assertEqual(
                    "sqlite-run", registry.run_detail("sqlite-run")["run_id"]
                )
                messages = registry.stream_events_since("sqlite-run", 0)
                dashboard_messages = [
                    row for row in messages if row["kind"] == "status"
                ]
                self.assertEqual(1, len(dashboard_messages))
                self.assertEqual(
                    "starting", dashboard_messages[0]["data"]["stage"]
                )
            finally:
                hub.shutdown(2.0)


if __name__ == "__main__":
    unittest.main()
