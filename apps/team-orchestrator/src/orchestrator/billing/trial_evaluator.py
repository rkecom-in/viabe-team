"""VT-90 — deterministic trial-lifecycle evaluator (zero-LLM, Pillar 1).

Decides, for one tenant at `now`, what the trial sweep should do:
  - ``warn``    — day-12 (trial_end - warn_lead): send the trial-ending warning.
  - ``extend``  — at trial-end, ENGAGED + under the extension cap: grant +14 days.
  - ``exhaust`` — at trial-end + grace, not extendable + no card: cancel (grace over).
  - ``none``    — nothing due (mid-trial, in grace, already paid, or terminal).

Engagement = ≥ `engagement_min_campaigns` campaigns in {approved, sent} generated
inside the trial window (config/trial.yaml — tunable). Trial end =
trial_started_at + trial_days * (1 + trial_extension_count). Pure SQL; no LLM.
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

TrialDecision = Literal["warn", "extend", "exhaust", "none"]


@dataclass(frozen=True)
class TrialVerdict:
    tenant_id: UUID
    decision: TrialDecision
    engaged: bool
    extension_count: int
    trial_end: datetime | None
    decided_at: datetime


def _config() -> dict[str, Any]:
    import yaml

    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def evaluate_trial(tenant_id: UUID | str, now: datetime | None = None) -> TrialVerdict:
    """Deterministic trial decision for one tenant. NO LLM."""
    now = now or datetime.now(timezone.utc)
    cfg = _config()
    trial_days = int(cfg["trial_days"])
    grace_days = int(cfg["grace_days"])
    warn_lead = int(cfg["warn_lead_days"])
    engage_min = int(cfg["engagement_min_campaigns"])
    max_ext = int(cfg["max_trial_extensions"])

    from orchestrator.graph import get_pool
    from psycopg.rows import dict_row

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT phase, trial_started_at, trial_extension_count, paid_conversion_at "
            "FROM tenants WHERE id = %s", (str(tenant_id),),
        )
        t = cur.fetchone()
        # Only active, unpaid trials are in scope.
        if (
            t is None
            or t["phase"] not in ("trial", "trial_extended")
            or t["paid_conversion_at"] is not None
            or t["trial_started_at"] is None
        ):
            return TrialVerdict(
                _uuid(tenant_id), "none", False,
                int(t["trial_extension_count"]) if t else 0, None, now,
            )

        count = int(t["trial_extension_count"])
        trial_start = t["trial_started_at"]
        trial_end = trial_start + timedelta(days=trial_days * (1 + count))

        cur.execute(
            "SELECT count(*) AS n FROM campaigns WHERE tenant_id = %s "
            "AND status IN ('approved', 'sent') "
            "AND generated_at >= %s AND generated_at < %s",
            (str(tenant_id), trial_start, trial_end),
        )
        engaged = int(cur.fetchone()["n"]) >= engage_min

    if now >= trial_end:
        if engaged and count < max_ext:
            decision: TrialDecision = "extend"
        elif now >= trial_end + timedelta(days=grace_days):
            decision = "exhaust"
        else:
            decision = "none"  # in the grace window — wait
    elif now >= trial_end - timedelta(days=warn_lead):
        decision = "warn"
    else:
        decision = "none"

    return TrialVerdict(_uuid(tenant_id), decision, engaged, count, trial_end, now)


def _uuid(x: UUID | str) -> UUID:
    return x if isinstance(x, UUID) else UUID(str(x))


__all__ = ["TrialDecision", "TrialVerdict", "evaluate_trial"]
