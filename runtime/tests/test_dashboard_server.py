from __future__ import annotations

import threading
import unittest
from urllib.request import urlopen
from pathlib import Path

from swarm_brain.runtime.dashboard_server import (
    DashboardHTTPServer,
    LiveRun,
    RunRegistry,
    _parse_run_path,
)


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
        self.assertIn("Blackboard 事件流", html)
        self.assertIn("PRIVATE SESSION", html)
        self.assertIn('message.kind === "agent_session"', html)
        self.assertEqual(("abc", "events"), _parse_run_path("/api/runs/abc/events"))
        self.assertEqual(("abc", None), _parse_run_path("/api/runs/abc"))

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
        self.assertIn("协同层实时看板", html)
        self.assertIn('"status": "ok"', health)


if __name__ == "__main__":
    unittest.main()
