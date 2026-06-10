"""Deterministic billing surface (VT-175).

Attribution-close aggregator (T+7 ARRR roll-up per campaign). Pillar 1
(revised 2026-05-12): this path is pure SQL + Python comparison.
**ZERO LLM invocations.** Enforced structurally by the
`gate-no-llm-in-deterministic-triggers` CI gate which scans every
function body in this package.

VT-365 (Fazal 2026-06-09): the day-39 fees-vs-ARRR evaluator + the whole money
clawback subsystem are REMOVED (trial simply expires to `lapsed`; owners opt in
to pay, never auto-charged, never clawed back). Only the attribution-close
aggregator survives on this surface.
"""

from orchestrator.billing.attribution_close import close_attribution
from orchestrator.billing.types import AttributionCloseResult

__all__ = [
    "AttributionCloseResult",
    "close_attribution",
]
