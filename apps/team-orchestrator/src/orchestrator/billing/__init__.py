"""Deterministic billing surface (VT-175).

Day-39 ARRR-vs-fees evaluator + attribution-close aggregator. Pillar 1
(revised 2026-05-12): these paths are pure SQL + Python comparison.
**ZERO LLM invocations.** Enforced structurally by the
`gate-no-llm-in-deterministic-triggers` CI gate which scans every
function body in this package.

Consumers
---------
- VT-176 (next row) wires `scheduled_triggers.py`'s shell bodies to
  invoke these.
- The Rule-#15 canary at `canaries/vt175_attributions_and_day39.py`
  exercises both branches end-to-end without sourcing `anthropic.env`
  (defense-in-depth — proves nothing in this path can reach an LLM
  even if a future caller tries).
"""

from orchestrator.billing.attribution_close import close_attribution
from orchestrator.billing.day39_evaluator import evaluate_day39
from orchestrator.billing.types import AttributionCloseResult, Day39Verdict

__all__ = [
    "AttributionCloseResult",
    "Day39Verdict",
    "close_attribution",
    "evaluate_day39",
]
