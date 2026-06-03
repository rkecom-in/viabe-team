"""VT-70 — retrieve_l4_skills standalone tool (MCP-wrappable).

The agent calls this to ground a decision in hand-authored domain knowledge (the
L4 corpus). Returns FULL doc bodies (the Composer bundle carries only excerpts +
titles; this tool is the on-demand full-text fetch). Counts as one tool call.

Pydantic IO; standalone callable. Embeds the query (voyage-4-lite) + pgvector
ANN over l4_documents (knowledge.l4_query). NO PII — the corpus is global,
hand-authored domain wisdom (Pillar 4/5).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class L4Skill(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    body: str
    tags: list[str]
    priority: int
    score: float | None = None


class RetrieveL4SkillsInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str = Field(..., min_length=1)
    business_type: str | None = None
    city_tier: str | None = None
    top_k: int = Field(default=5, ge=1, le=10)


class RetrieveL4SkillsOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    skills: list[L4Skill]


def retrieve_l4_skills(payload: RetrieveL4SkillsInput) -> RetrieveL4SkillsOutput:
    """Retrieve the top-k applicable L4 corpus docs (full bodies) for the query.

    Empty list when the corpus is empty (real docs load via VT-313). Embedding
    failures propagate (DR-15 fail-not-skip) — the caller decides whether to
    proceed without L4; the Composer's _build_l4_skills already degrades to the
    no-skills marker for the always-on bundle path.
    """
    from orchestrator.knowledge.l4_query import retrieve_documents

    docs = retrieve_documents(
        payload.query,
        business_type=payload.business_type,
        city_tier=payload.city_tier,
        top_k=payload.top_k,
    )
    return RetrieveL4SkillsOutput(
        skills=[
            L4Skill(
                title=d.title, body=d.body, tags=d.tags,
                priority=d.priority, score=d.score,
            )
            for d in docs
        ]
    )


__all__ = [
    "L4Skill", "RetrieveL4SkillsInput", "RetrieveL4SkillsOutput",
    "retrieve_l4_skills",
]
