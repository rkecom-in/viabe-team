"""VT-288 — cold-start ramp governor.

A new WABA sending into a cold list trips Meta preemptive enforcement (rapid-list-growth
/ high-velocity-low-engagement). This governor gates daily HOOK send volume per tenant on
observed engagement: start small (tier 0), step up only as the opt-in/reply rate clears a
bar AND quality hasn't dropped. Config-driven (config/ramp_governor.yaml) so Fazal tunes
without a deploy. The cap + the decision are returned (observable; feeds VT-296 later).

Pure functions — no DB, no network. The caller supplies the current tier + measured
engagement; this decides the cap + the next tier. Deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_CONFIG = Path(__file__).resolve().parent.parent.parent.parent / "config" / "ramp_governor.yaml"


@lru_cache(maxsize=1)
def _config() -> dict[str, Any]:
    return yaml.safe_load(_CONFIG.read_text())


@dataclass(frozen=True, slots=True)
class RampDecision:
    """The governor's output — observable (cap + tier + why)."""

    tier: int                 # resulting tier index after the decision
    daily_cap: int            # max hook sends allowed today for this WABA
    action: str               # hold | promote | demote
    reason: str


def decide(
    current_tier: int,
    engagement_rate: float,
    sends_in_window: int,
    *,
    quality_dropped: bool = False,
) -> RampDecision:
    """Decide today's cap + tier from the current tier + measured engagement.

    - quality drop OR engagement < demote_below  -> step DOWN one tier.
    - engagement >= threshold AND enough sends    -> step UP one tier (capped at top).
    - else                                        -> hold.
    """
    cfg = _config()
    tiers: list[int] = list(cfg["tiers"])
    top = len(tiers) - 1
    tier = max(0, min(int(current_tier), top))
    threshold = float(cfg["engagement_threshold"])
    demote_below = float(cfg["demote_below"])
    min_sends = int(cfg["min_sends_before_step"])

    if quality_dropped or engagement_rate < demote_below:
        new_tier = max(0, tier - 1)
        action = "demote" if new_tier != tier else "hold"
        reason = (
            "quality rating dropped" if quality_dropped
            else f"engagement {engagement_rate:.0%} < demote floor {demote_below:.0%}"
        )
        return RampDecision(new_tier, tiers[new_tier], action, reason)

    if engagement_rate >= threshold and sends_in_window >= min_sends and tier < top:
        new_tier = tier + 1
        return RampDecision(
            new_tier, tiers[new_tier], "promote",
            f"engagement {engagement_rate:.0%} >= {threshold:.0%} over {sends_in_window} sends",
        )

    # hold (incl. cold start at tier 0 before min_sends accrue)
    if sends_in_window < min_sends:
        reason = f"only {sends_in_window} sends in-window (< {min_sends}); holding tier {tier}"
    else:
        reason = f"engagement {engagement_rate:.0%} < promote bar {threshold:.0%}; holding"
    return RampDecision(tier, tiers[tier], "hold", reason)


def cold_start_cap() -> int:
    """The tier-0 daily cap (the cap a brand-new WABA starts at)."""
    return int(_config()["tiers"][0])


__all__ = ["RampDecision", "decide", "cold_start_cap"]
