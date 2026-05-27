"""VT-126 Context Composer hook for orchestrator-agent: render L0 priors.

The orchestrator-agent uses langchain's ``ChatAnthropic`` and consumes a
message stream — NOT the ``SalesRecoveryContext`` bundle that
``context_builder.py`` produces (Pillar 1: agent code MUST NOT import the
specialist bundle module; lint-enforced).

So the Context Composer hook for orchestrator-agent priors lives here:
``build_l0_prior_observations(business_type, city_tier, current_phase)``
queries ``query_l0`` for the three fragment_types under the synthesised
cohort_key and renders a compact markdown block. Callers prepend the
block to the orchestrator-agent's first user message before invoking
``OrchestratorAgentDriver``.

Budget (per VT-126 brief): up to 1K tokens of the 4K orchestrator-agent
input cap. The renderer caps at ``_FRAGMENT_BUDGET_CHARS`` (≈ 4 chars /
token × 1000 tokens × safety 0.95) and truncates extra fragments
gracefully. The block is empty when no fragments pass the k>=10 RLS
gate — callers can treat it as no-op when the cohort hasn't accumulated
enough observations yet.
"""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.observability.l0_memory import FragmentType, query_l0

logger = logging.getLogger(__name__)


# 4 chars / token × ~1000-token budget × 0.95 safety. The orchestrator-
# agent's 4K input cap allows 1K for priors; everything beyond truncates.
_FRAGMENT_BUDGET_CHARS = 3800

_FRAGMENT_TYPES: tuple[FragmentType, ...] = (
    "routing_decision",
    "specialist_outcome",
    "trigger_pattern",
)


def build_cohort_key(
    *, business_type: str, city_tier: str, current_phase: str
) -> str:
    """Synthesise the L0 cohort_key. NEVER include tenant_id (CL-390)."""
    return f"{business_type}|{city_tier}|{current_phase}"


def build_l0_prior_observations(
    *,
    business_type: str,
    city_tier: str,
    current_phase: str,
    k_per_type: int = 3,
) -> str:
    """Render up to ``k_per_type * 3`` L0 fragments as a markdown block.

    Returns an empty string when no fragments pass the k-anonymity gate
    for any of the three fragment_types — callers can prepend
    unconditionally (empty string is a no-op concat).

    The rendered shape is fixed:

        ## Prior cohort observations (cohort=<key>)
        ### routing_decision
        - obs=42 last=2026-05-20T... | {content}
        ...

    Token-budget enforcement: assembled block trimmed when characters
    exceed ``_FRAGMENT_BUDGET_CHARS``; truncation is fragment-boundary
    (no mid-line splits) and emits a ``(... N more truncated)`` marker
    so callers can observe the cap was hit.
    """
    cohort_key = build_cohort_key(
        business_type=business_type,
        city_tier=city_tier,
        current_phase=current_phase,
    )

    lines: list[str] = [f"## Prior cohort observations (cohort={cohort_key})"]
    any_rendered = False
    for ftype in _FRAGMENT_TYPES:
        try:
            result = query_l0(
                fragment_type=ftype, cohort_key=cohort_key, k=k_per_type
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "VT-126 query_l0 failed; rendering empty section",
                extra={
                    "fragment_type": ftype,
                    "cohort_key": cohort_key,
                    "exc": repr(exc),
                },
            )
            continue
        fragments: list[dict[str, Any]] = result.get("fragments", [])
        if not fragments:
            continue
        any_rendered = True
        lines.append(f"### {ftype}")
        for frag in fragments:
            lines.append(
                f"- obs={frag.get('observation_count')} "
                f"last={frag.get('last_observed_at')} | "
                f"{frag.get('content')}"
            )

    if not any_rendered:
        return ""

    rendered = "\n".join(lines)
    if len(rendered) <= _FRAGMENT_BUDGET_CHARS:
        return rendered

    # Trim to budget at line boundary; append truncation marker so the
    # cap-hit is visible in the agent's input.
    kept: list[str] = []
    char_count = 0
    truncated_count = 0
    for line in lines:
        # +1 for the join newline.
        cost = len(line) + 1
        if char_count + cost > _FRAGMENT_BUDGET_CHARS:
            truncated_count += 1
            continue
        kept.append(line)
        char_count += cost
    if truncated_count > 0:
        kept.append(f"(... {truncated_count} more truncated for budget)")
    return "\n".join(kept)


__all__ = [
    "build_cohort_key",
    "build_l0_prior_observations",
]
