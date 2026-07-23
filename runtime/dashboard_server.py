"""Local real-time dashboard for the formal coordination runtime.

The server uses only Python's standard library.  It binds to localhost by
default, keeps DeepSeek credentials server-side, and streams Blackboard events
to the browser with Server-Sent Events.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from .coordination_runtime import CoordinationRuntime


MAX_REQUEST_BYTES = 64 * 1024


@dataclass
class LiveRun:
    run_id: str
    instruction: str
    messages: list[dict] = field(default_factory=list)
    done: bool = False
    result: Optional[dict] = None
    error: Optional[str] = None
    _condition: threading.Condition = field(
        default_factory=threading.Condition, repr=False
    )

    def publish(self, kind: str, data: dict) -> None:
        with self._condition:
            message = {
                "seq": len(self.messages) + 1,
                "kind": kind,
                "at": time.time(),
                "data": data,
            }
            self.messages.append(message)
            self._condition.notify_all()

    def finish(
        self, *, result: Optional[dict] = None, error: Optional[str] = None
    ) -> None:
        with self._condition:
            self.result = result
            self.error = error
            self.done = True
            self._condition.notify_all()

    def wait_after(self, seq: int, timeout_s: float) -> tuple[list[dict], bool]:
        with self._condition:
            if len(self.messages) <= seq and not self.done:
                self._condition.wait(timeout_s)
            return list(self.messages[seq:]), self.done

    def snapshot(self) -> dict:
        with self._condition:
            return {
                "run_id": self.run_id,
                "instruction": self.instruction,
                "message_count": len(self.messages),
                "done": self.done,
                "result": self.result,
                "error": self.error,
            }


class RunRegistry:
    def __init__(
        self,
        runtime_factory: Callable[[], CoordinationRuntime] = CoordinationRuntime,
    ) -> None:
        self.runtime_factory = runtime_factory
        self._runs: dict[str, LiveRun] = {}
        self._lock = threading.Lock()

    def start(self, instruction: str) -> LiveRun:
        with self._lock:
            if any(not item.done for item in self._runs.values()):
                raise RuntimeError("已有任务正在运行，请等待当前任务结束")
            run_id = uuid.uuid4().hex
            state = LiveRun(run_id=run_id, instruction=instruction)
            self._runs[run_id] = state
        thread = threading.Thread(
            target=self._execute,
            args=(state,),
            name=f"coordination-dashboard-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return state

    def get(self, run_id: str) -> Optional[LiveRun]:
        with self._lock:
            return self._runs.get(run_id)

    def _execute(self, state: LiveRun) -> None:
        try:
            runtime = self.runtime_factory()
            result = runtime.run(
                state.instruction,
                event_listener=lambda data: state.publish(
                    "blackboard_event", data
                ),
                status_listener=lambda data: state.publish("status", data),
                llm_listener=lambda data: state.publish("llm_call", data),
                session_listener=lambda data: state.publish(
                    "agent_session", data
                ),
            )
            state.publish("result", result)
            state.finish(result=result)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            state.publish("error", {"message": error})
            state.finish(error=error)


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        registry: Optional[RunRegistry] = None,
    ) -> None:
        super().__init__(server_address, DashboardHandler)
        self.registry = registry or RunRegistry()
        self.dashboard_html = (
            Path(__file__).with_name("dashboard.html").read_bytes()
        )


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: DashboardHTTPServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/dashboard.html"):
            self._send_bytes(
                HTTPStatus.OK,
                self.server.dashboard_html,
                "text/html; charset=utf-8",
                cache=False,
            )
            return
        if parsed.path == "/favicon.ico":
            self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return
        if parsed.path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "service": "coordination-dashboard"},
            )
            return

        run_id, suffix = _parse_run_path(parsed.path)
        if run_id is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        state = self.server.registry.get(run_id)
        if state is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
            return
        if suffix == "events":
            since_value = parse_qs(parsed.query).get("since", ["0"])[0]
            try:
                since = max(0, int(since_value))
            except ValueError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST, {"error": "invalid_since"}
                )
                return
            self._stream_events(state, since)
            return
        if suffix is None:
            self._send_json(HTTPStatus.OK, state.snapshot())
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        if urlparse(self.path).path != "/api/runs":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"}
            )
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return
        instruction = body.get("instruction") if isinstance(body, dict) else None
        if not isinstance(instruction, str) or not instruction.strip():
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "instruction_required"}
            )
            return
        if len(instruction) > 1000:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "instruction_too_long"}
            )
            return
        try:
            state = self.server.registry.start(instruction.strip())
        except RuntimeError as exc:
            self._send_json(
                HTTPStatus.CONFLICT, {"error": "run_in_progress", "message": str(exc)}
            )
            return
        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "run_id": state.run_id,
                "events_url": f"/api/runs/{state.run_id}/events",
            },
        )

    def _stream_events(self, state: LiveRun, since: int) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        seq = since
        try:
            while True:
                messages, done = state.wait_after(seq, 15.0)
                if messages:
                    for message in messages:
                        payload = json.dumps(
                            message, ensure_ascii=False, separators=(",", ":")
                        )
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                        seq = int(message["seq"])
                    self.wfile.flush()
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                if done and not state.wait_after(seq, 0)[0]:
                    break
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.close_connection = True

    def _send_json(self, status: HTTPStatus, value: dict) -> None:
        self._send_bytes(
            status,
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            cache=False,
        )

    def _send_bytes(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
        *,
        cache: bool = True,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'",
        )
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        print(f"[dashboard] {self.address_string()} - {format % args}")


def _parse_run_path(path: str) -> tuple[Optional[str], Optional[str]]:
    parts = [item for item in path.split("/") if item]
    if len(parts) == 3 and parts[:2] == ["api", "runs"]:
        return parts[2], None
    if (
        len(parts) == 4
        and parts[:2] == ["api", "runs"]
        and parts[3] == "events"
    ):
        return parts[2], "events"
    return None, None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve the local SwarmBrain coordination dashboard"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be within 1..65535")
    server = DashboardHTTPServer((args.host, args.port))
    print(f"Coordination dashboard: http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
