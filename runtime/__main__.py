"""Command line entrypoint for the coordination-v2 runtime."""
from __future__ import annotations

import argparse
import json

from .coordination_runtime import CoordinationRuntime


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one complete SwarmBrain coordination-v2 task"
    )
    parser.add_argument("instruction", help="one-sentence user instruction")
    args = parser.parse_args()
    result = CoordinationRuntime().run(args.instruction)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
