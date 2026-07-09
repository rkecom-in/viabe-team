"""VT-90 / VT-365 — deterministic trial-lifecycle evaluator (zero-LLM, Pillar 1).

Decides, for one tenant at `now`, what the trial sweep should do:
  - ``warn``    — at ``trial_end - warn_lead``: send the trial-ending warning.
  - ``expire``  — at/after ``trial_end``: the 30-day trial elapsed without an
                  explicit owner ``subscribe`` → emit ``trial_expired`` (phase
                  ``trial`` → dormant ``lapsed``).
  - ``none``    — nothing due (mid-trial, already subscribed, or not in a trial).

VT-365 (Fazal 2026-06-09): 30-day free trial, NO card in trial, owner opt-in
``subscribe`` at/after day 30, NO auto-charge, no money clawback. The old
engagement-gated auto-extension + post-grace cancel path is REMOVED — a trial
now simply EXPIRES to ``lapsed``. Trial end = ``trial_started_at + trial_days``
(``config/trial.yaml`` — tunable). Pure SQL; no LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

logger = logging.getLogger(__name__)

# .../team-orchestrator/src/orchestrator/billing/trial_evaluator.py → parents[3]
_CONFIG = Path(__file__).resolve().parents[3] / "config" / "trial.yaml"

TrialDecision = Literal["warn", "expire", "none"]


@dataclass(frozen=True)
class TrialVerdict:
    tenant_id: UUID
    decision: TrialDecision
    trial_end: datetime | None
    decided_at: datetime


def _config() -> dict[str, Any]:
    import yaml

    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def trial_days() -> int:
    """The canonical free-trial length (``config/trial.yaml``). The SINGLE SOURCE for the trial
    window — owner-facing surfaces (e.g. the dashboard's ``trial_ends``) MUST read this, never a
    hardcoded 30 (VT-371 drift class: signup already drifted once on a hardcoded trial length)."""
    return int(_config()["trial_days"])


def evaluate_trial(tenant_id: UUID | str, now: datetime | None = None) -> TrialVerdict:
    """Deterministic trial decision for one tenant. NO LLM."""
    now = now or datetime.now(timezone.utc)
    cfg = _config()
    trial_days = int(cfg["trial_days"])
    warn_lead = int(cfg["warn_lead_days"])

    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT phase, trial_started_at, paid_conversion_at "
            "FROM tenants WHERE id = %s", (str(tenant_id),),
        )
        t = cur.fetchone()
        # Only active, un-subscribed trials are in scope.
        if (
            t is None
            or t["phase"] != "trial"
            or t["paid_conversion_at"] is not None
            or t["trial_started_at"] is None
        ):
            return TrialVerdict(_uuid(tenant_id), "none", None, now)

        trial_start = t["trial_started_at"]
        trial_end = trial_start + timedelta(days=trial_days)

    if now >= trial_end:
        decision: TrialDecision = "expire"
    elif now >= trial_end - timedelta(days=warn_lead):
        decision = "warn"
    else:
        decision = "none"

    return TrialVerdict(_uuid(tenant_id), decision, trial_end, now)


def _uuid(x: UUID | str) -> UUID:
    return x if isinstance(x, UUID) else UUID(str(x))


__all__ = ["TrialDecision", "TrialVerdict", "evaluate_trial"]
