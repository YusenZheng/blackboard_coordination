"""Compatibility entrypoint for the supported coordination-v2 chain 1.

This module intentionally contains no runtime implementation.  The former
``Harness/AgentLoop`` walking skeleton was a second executable chain and made
it ambiguous which contract users were exercising.  All execution now
delegates to :class:`CoordinationRuntime`.
"""
from __future__ import annotations

from typing import Any

from .__main__ import main
from .coordination_runtime import CoordinationRuntime


def run(instruction: str, **kwargs: Any) -> dict:
    """Run V2 chain 1 and return its structured result."""

    return CoordinationRuntime(**kwargs).run(instruction)


if __name__ == "__main__":
    main()
