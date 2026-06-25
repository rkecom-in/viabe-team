"""VT-421 — the agent activation-prerequisites REGISTRY.

Fazal (2026-06-25, PINNED + EXPANDED): an agent (Sales-Recovery today; more tomorrow) executes
ONLY for a tenant that has crossed that agent's activation bar. The bar is no longer a hardcoded
SR condition buried in the gate — it is a DECLARATIVE, per-agent prerequisite set that the gate
READS. A future agent declares its own prereqs HERE, with ZERO change to the gate logic.

WHY A CODE REGISTRY (not a DB table) — same call as ``integrations/registry.py``:
  - Activation prerequisites are part of the PRODUCT's behavioral contract: an agent's bar ships
    WITH the agent's code and changes only on a deliberate, reviewed code change.
  - Version-controlled + diffable + unit-testable at boot (one test asserts every entry is
    structurally valid) — a DB table would add an RLS surface, a migration, and a
    deploy-vs-data-skew gap (code says X, prod table says Y) for ZERO live-ops benefit. There is no
    owner/ops "edit a prereq at runtime" use-case.
  - The gate stays a thin EVALUATOR over this data; extensibility lives in the data, not in
    branching code. Mirrors the REGISTRY precedent (one entry per supported thing, looked up by id).

SHAPE — one ``AgentPrerequisites`` (frozen dataclass) per agent, keyed by agent name in
``REGISTRY``. Prereqs are DATA (booleans / ints), each carrying a stable ``code`` + human-readable
``reason`` so the gate can answer BOTH "is this agent active?" (a boolean) AND "WHY is it inactive?"
(``unmet_prerequisites`` → a list the owner-facing portal renders). The tenant-fact reads stay in
``onboarding_gate`` (the gate owns SQL + fail-closed); the registry owns only the DECLARATION of
which facts each agent requires + at what threshold.

ADDING A FUTURE AGENT (no gate edit):
    REGISTRY["my_new_agent"] = AgentPrerequisites(
        agent="my_new_agent",
        requires_journey_complete=True,        # onboarding_journey.status='complete'
        requires_verification=True,            # verification_status >= gstin_verified
        requires_enabled_data_source=False,    # this agent doesn't need an ingest connector
        min_customers=0,                        # …nor ingested customers
    )
The gate iterates whatever the entry declares — a flag set False simply drops that prereq from the
evaluated set for that agent.

CL-390: declarations only — NO tenant data, IDs, names, or facts live here.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Stable prerequisite codes (the machine key the portal/tests pin on) -------------------------
# These are the UNMET-reason codes ``unmet_prerequisites`` emits. Stable strings — a UI/owner-facing
# surface keys on them; do not rename without updating the consumer.
PREREQ_JOURNEY_COMPLETE = "onboarding_incomplete"
PREREQ_VERIFICATION = "verification_below_gstin"
PREREQ_DATA_SOURCE = "no_connected_data_source"
PREREQ_CUSTOMERS = "no_customers_ingested"

# Human-readable reasons paired with each code (rendered in the owner portal "why inactive" surface).
_PREREQ_REASONS: dict[str, str] = {
    PREREQ_JOURNEY_COMPLETE: "onboarding not complete",
    PREREQ_VERIFICATION: "GSTIN not verified",
    PREREQ_DATA_SOURCE: "no connected customer-data source",
    PREREQ_CUSTOMERS: "no customers ingested",
}


def prereq_reason(code: str) -> str:
    """Human-readable reason for a prerequisite code (falls back to the code itself)."""
    return _PREREQ_REASONS.get(code, code)


@dataclass(frozen=True)
class AgentPrerequisites:
    """The declarative activation bar for ONE agent.

    Each field is a prerequisite the gate evaluates against tenant facts. A flag set ``False`` (or
    ``min_customers=0``) DROPS that prerequisite for this agent — that is how a future agent with a
    different bar declares itself without touching the gate. The gate reads these and resolves each
    declared prereq against the tenant's live state, fail-closed.

    Fields:
      - ``agent``                        — the agent key (must match the REGISTRY key).
      - ``requires_journey_complete``    — gate on ``onboarding_journey.status='complete'`` (admits
                                           BOTH trial AND paid: the 1-month free trial is deliberately
                                           UNRESTRICTED — the bar is journey-complete, NOT paid).
      - ``requires_verification``        — gate on ``verification_status >= gstin_verified``.
      - ``requires_enabled_data_source`` — gate on ≥1 ENABLED customer-data source that has pulled
                                           data (any ingest connector: shopify | google_sheet | csv |
                                           …; generalized — NOT shopify-specific).
      - ``min_customers``                — minimum ingested ``customers`` count (0 = no requirement).
    """

    agent: str
    requires_journey_complete: bool = True
    requires_verification: bool = True
    requires_enabled_data_source: bool = False
    min_customers: int = 0


# === The registry — one entry per agent ========================================================
#
# SR's entry (Fazal-pinned): journey-complete AND verification>=gstin_verified AND ≥1 enabled
# customer-data source (ANY ingest connector) AND customers>=1.
REGISTRY: dict[str, AgentPrerequisites] = {
    "sales_recovery": AgentPrerequisites(
        agent="sales_recovery",
        requires_journey_complete=True,
        requires_verification=True,
        requires_enabled_data_source=True,
        min_customers=1,
    ),
}


def get_prerequisites(agent: str) -> AgentPrerequisites:
    """Look up an agent's activation prerequisites. Raises ``KeyError`` on an unknown agent.

    Fail-closed posture: an UNREGISTERED agent has no declared bar, so the gate treats a KeyError as
    ineligible (it never silently admits an agent we never declared a bar for) — see
    ``onboarding_gate.is_agent_eligible``.
    """
    if agent not in REGISTRY:
        raise KeyError(
            f"agent '{agent}' not in activation registry; available: {sorted(REGISTRY.keys())}"
        )
    return REGISTRY[agent]


__all__ = [
    "PREREQ_CUSTOMERS",
    "PREREQ_DATA_SOURCE",
    "PREREQ_JOURNEY_COMPLETE",
    "PREREQ_VERIFICATION",
    "AgentPrerequisites",
    "REGISTRY",
    "get_prerequisites",
    "prereq_reason",
]
