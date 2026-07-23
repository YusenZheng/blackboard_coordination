"""Explainable matching between AgentCard capabilities and Tool requirements."""
from __future__ import annotations

from ..contracts.agent_card import AgentCard
from ..contracts.tooling import CapabilityMatchResult, CapabilityRequirement


class CapabilityMatcher:
    def match(
        self, card: AgentCard, requirement: CapabilityRequirement
    ) -> CapabilityMatchResult:
        capabilities = set(card.capability.profile.capabilities)
        missing = sorted(requirement.all_of - capabilities)
        unsatisfied = [
            sorted(group)
            for group in requirement.any_of
            if group and not capabilities.intersection(group)
        ]
        reasons: list[str] = []
        if missing:
            reasons.append("REQUIRED_CAPABILITY_MISSING")
        if unsatisfied:
            reasons.append("ALTERNATIVE_CAPABILITY_MISSING")
        return CapabilityMatchResult(
            matched=not reasons,
            missing_capabilities=missing,
            unsatisfied_any_of=unsatisfied,
            reason_codes=reasons,
        )
