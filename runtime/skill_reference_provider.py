"""Runtime adapter from the asset Skill registry to coordination V2 DTOs."""
from __future__ import annotations

from ..assets.skill import SkillGraph
from ..coordination.models import SkillQuery, SkillReference


class AssetSkillReferenceProvider:
    def __init__(self, graph: SkillGraph, tool_catalog) -> None:
        self.graph = graph
        self.tool_catalog = tool_catalog

    def search(self, query: SkillQuery, limit: int = 3) -> list[SkillReference]:
        available = {
            spec.tool_id for spec in self.tool_catalog.list() if spec.implemented
        }
        skills = self.graph.search(
            task_type=query.task_type,
            situation_tags=query.situation_tags,
            capability_ids=query.capability_ids,
            available_tool_ids=available,
            limit=limit,
        )
        return [
            SkillReference(
                skill_id=skill.signature,
                version=str(skill.extra.get("version", "1.0.0")),
                title=str(skill.extra.get("title", skill.signature)),
                strategy_summary=skill.strategy,
                applicable_conditions=list(skill.preconditions),
                source_ref=f"assets://skills/{skill.signature}",
                tool_chain=[tool_id for tool_id in skill.tool_chain if tool_id in available],
            )
            for skill in skills
        ]
