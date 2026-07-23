"""V2 emergence contracts and candidate lifecycle for the MVP boundary.

The MVP records sufficient traces and freezes these ports. It deliberately does
not auto-generate, validate, or publish an L2 Skill.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from .skill import SkillCandidate


class CandidateState(str, Enum):
    CANDIDATE = "candidate"
    CANDIDATE_TRIAL = "candidate_trial"
    VALIDATED = "validated"
    REGISTERED = "registered"
    REJECTED = "rejected"
    CANARY = "canary"
    ROLLED_BACK = "rolled_back"
    RETIRED = "retired"


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    metric_values: dict = field(default_factory=dict)
    reason_codes: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AdmissionDecision:
    approved: bool
    target_state: CandidateState
    reason_codes: list[str] = field(default_factory=list)


class PatternDetectorPort(Protocol):
    def detect(self, traces: list[dict]) -> list[SkillCandidate]: ...


class SimulationEvaluatorPort(Protocol):
    def evaluate(
        self, candidate: SkillCandidate, scenarios: list[dict]
    ) -> ValidationResult: ...


class AdmissionGatePort(Protocol):
    def evaluate(
        self, candidate: SkillCandidate, validation: ValidationResult
    ) -> AdmissionDecision: ...

    def rollback(self, skill_id: str, version: str, reason: str) -> None: ...


class CandidateLifecycle:
    """Validate lifecycle transitions without pretending to perform admission."""

    _allowed = {
        CandidateState.CANDIDATE: {
            CandidateState.CANDIDATE_TRIAL,
            CandidateState.REJECTED,
        },
        CandidateState.CANDIDATE_TRIAL: {
            CandidateState.VALIDATED,
            CandidateState.REJECTED,
        },
        CandidateState.VALIDATED: {CandidateState.REGISTERED},
        CandidateState.REGISTERED: {
            CandidateState.CANARY,
            CandidateState.RETIRED,
        },
        CandidateState.CANARY: {
            CandidateState.REGISTERED,
            CandidateState.ROLLED_BACK,
        },
    }

    def transition(
        self, candidate: SkillCandidate, target: CandidateState
    ) -> SkillCandidate:
        current = CandidateState(candidate.lifecycle_status)
        if target not in self._allowed.get(current, set()):
            raise ValueError(f"invalid candidate transition: {current.value}->{target.value}")
        candidate.lifecycle_status = target.value
        return candidate
