"""Persistent memory for the V2 coordination runtime.

The memory package owns two kinds of long-lived knowledge:

* private working/episodic records, always scoped to one agent;
* reviewed shared semantic facts and pending fact/skill candidates.

Blackboard events, raw traces and registered skills remain owned by their
respective modules.  This package only stores provenance references to them.
"""

from .consolidation import (
    MemoryConsolidator,
    TaskEpisodeBuilder,
    TraceAssetQualityGate,
)
from .models import (
    CandidateDecision,
    CandidateStatus,
    CandidateType,
    ConsolidationResult,
    Provenance,
    QualityGateResult,
    TraceAsset,
    TraceAssetStatus,
    TraceEventRef,
)
from .store import NoOpMemory, SQLiteMemoryStore, default_memory_path

__all__ = [
    "CandidateDecision",
    "CandidateStatus",
    "CandidateType",
    "ConsolidationResult",
    "MemoryConsolidator",
    "NoOpMemory",
    "Provenance",
    "QualityGateResult",
    "SQLiteMemoryStore",
    "TaskEpisodeBuilder",
    "TraceAsset",
    "TraceAssetQualityGate",
    "TraceAssetStatus",
    "TraceEventRef",
    "default_memory_path",
]
