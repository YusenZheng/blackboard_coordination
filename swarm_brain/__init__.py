"""Compatibility package for the repository's flat source layout.

The project historically keeps ``coordination/``, ``blackboard/`` and the other
subpackages at repository root while importing them as ``swarm_brain.*``.  By
pointing this package's search path at that root, documented ``python -m
swarm_brain...`` commands work without renaming the checkout directory.
"""
from pathlib import Path

__path__ = [str(Path(__file__).resolve().parent.parent)]
__version__ = "0.1.0"
