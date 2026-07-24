"""Command line entrypoint for the coordination-v2 runtime."""
from __future__ import annotations

import argparse
import json
import logging

from ..memory import NoOpMemory, SQLiteMemoryStore
from ..observability import ObservabilityHub
from .coordination_runtime import CoordinationRuntime


logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one complete SwarmBrain coordination-v2 task"
    )
    parser.add_argument("instruction", help="one-sentence user instruction")
    parser.add_argument(
        "--capture-mode",
        choices=("metadata", "errors", "full"),
        default="metadata",
        help="module input/output retention mode (default: metadata)",
    )
    args = parser.parse_args()
    observability = ObservabilityHub(capture_mode=args.capture_mode)
    try:
        memory = SQLiteMemoryStore()
    except Exception:
        logger.exception("memory store unavailable; using no-op memory")
        memory = NoOpMemory()
    try:
        result = CoordinationRuntime(
            observability=observability,
            memory=memory,
        ).run(args.instruction, capture_mode=args.capture_mode)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        memory.close()
        observability.shutdown()


if __name__ == "__main__":
    main()
