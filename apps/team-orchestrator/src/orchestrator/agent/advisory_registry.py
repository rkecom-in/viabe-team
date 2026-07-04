"""VT-604 Package 1 — the Manager-held ADVISORY tool registry.

The six business-domain lanes built under VT-468..473 (sales / marketing / finance /
accounting / tech / cost_opt) are NOT independent specialists (execution-plan Package 1,
``.viabe/plans/manager-loop-execution-plan.md`` §1 + §3): they hold no activation bar,
no durable task/plan participation, and no specialist-return handling of their own. They
were nonetheless wired as fully spawnable graph nodes (VT-465's ``_register_lanes``),
which is corrected here — see ``agent/roster.py``'s VT-604 note. ``ROSTER`` now holds
EXACTLY the three Phase-1 specialists (sales_recovery / integration /
onboarding_conductor); the six lanes are not graph nodes, have no spawn tool, and are
never described to the model as something it can hand off to.

Instead, this module exposes a CURATED SUBSET of the six lanes' own ``@tool``-decorated
functions DIRECTLY to the Manager's own tool inventory (``supervisor.build_supervisor_
graph`` adds ``ADVISORY_TOOLS`` to ``build_orchestrator_agent``'s ``extra_tools``,
alongside the three roster spawn tools). The Manager calls these itself, in its own
turn — there is no sub-graph, no handoff, no separate specialist reasoning about them.
This matches the six domains' actual v1 charter: every one of them was ALREADY
advisory-only by construction (VT-268 ``assert_agent_tools_safe`` forbids a send / spend
/ commit / config-write / ledger-write tool on any of their surfaces) — the only change
is WHO calls the tool (the Manager itself, not a spawned sub-agent) and HOW it is framed
to the model (an analyse/prepare/draft capability, not a delegate-to-a-specialist
handoff).

The six lane MODULES themselves are UNCHANGED. Their ``SPECIALIST_SPEC`` exports still
exist (their own per-lane tests still validate the shape), but nothing appends them to
``ROSTER`` anymore — that export is now unused-but-harmless dead code on the roster
spine, kept only as a documented seam should a lane ever graduate to a real specialist.

---------------------------------------------------------------------------------------
CLASSIFICATION — every one of the 23 tenant-scoped + 10 non-tenant-scoped lane tools
---------------------------------------------------------------------------------------

INCLUDED (advisory: read / analyse / prepare / draft, or a RAIL-FACING read-only probe
that reports a deterministic gate's decision without executing anything):

  sales_lane:
    - recommend_sales_play               — drafts a structured sales-play recommendation
                                            (intent only; no send). The manager reads the
                                            recommendation to decide whether to delegate
                                            winback to ``spawn_sales_recovery`` or draft
                                            further guidance itself.
    - identify_repeat_upsell_opportunity — pure reasoning-grounding read (no DB, no effect).

  marketing_lane:
    - list_recent_campaigns    — read-only rollup (counts only, CL-390).
    - draft_campaign_plan      — drafts a campaign/offer intent; no send.
    - draft_content            — drafts content copy; no send.
    - check_send_intent        — RAIL-FACING probe (reports the CUSTOMER_SEND policy bound;
                                  does not send).
    - check_ad_spend_intent    — RAIL-FACING probe (reports the SPEND business-impact gate;
                                  does not spend).

  finance_lane:
    - analyze_cash_flow         — read-only aggregate.
    - analyze_receivables       — read-only aggregate.
    - pricing_margin_input      — read-only aggregate.
    - propose_payment_reminder  — drafts a reminder PROPOSAL; no send/persist.

  accounting_lane (v1 PREPARE-only by charter — every tool qualifies):
    - accounting_categorize_books
    - accounting_prepare_tax_summary
    - accounting_organize_invoices_expenses
    - accounting_reconcile_transactions

  tech_lane:
    - read_integration_health     — read-only (tenant_connector_status).
    - read_listing_health         — read-only (platform_listings).
    - advise_integration_setup    — read-only registry advice (owner-visible catalogue
                                     only — VT-604 Package 1 connector filter).
    - read_tech_context           — read-only (business_context slice).
    - propose_config_change       — drafts a config-change intent; no write.
    - check_config_change_intent  — RAIL-FACING probe (reports the CONFIG business-impact
                                     gate; does not write).

  cost_opt_lane (v1 ADVISE-only by charter — every tool qualifies):
    - analyze_tenant_spend
    - analyze_unit_economics
    - identify_spend_anomaly
    - analyze_marketing_roi
    - read_cost_context

EXCLUDED, with reason:

  - ``push_back_to_manager`` (sales_lane), ``finance_pushback`` (finance_lane) — these
    implement the SPECIALIST -> MANAGER two-way handoff PUSHBACK protocol
    (``roster.SpecialistReturn(pushback=True, ...)`` — design §7): a spawned specialist
    telling the manager its framing is infeasible. That protocol requires a caller who
    is NOT the manager itself; once the manager calls its own tool directly, "push back
    to the manager" is meaningless (there is no separate judgment to report — the
    manager can simply decide not to act). Structurally orphaned, not effectful; excluded
    for coherence, not safety.
  - ``sales_lane_escalate_to_fazal`` / ``marketing_escalate_to_fazal`` /
    ``finance_escalate_to_fazal`` / ``accounting_escalate_to_fazal`` /
    ``tech_escalate_to_fazal`` — the Manager already holds its OWN ``escalate_to_fazal``
    (``orchestrator_agent.ORCHESTRATOR_AGENT_TOOLS``). Six near-duplicate per-lane
    escalate tools add no capability and only invite the model to pick the wrong one;
    excluded as redundant. (``cost_opt_lane`` never had its own escalate tool.)

No tool in this module sends, spends, commits, configures, or mutates external state —
every included tool is a pure read, a drafted proposal/intent, or a read-only rail probe.
This is enforced structurally (VT-268 ``assert_agent_tools_safe`` — Sends/writes are
named FORBIDDEN_CAPABILITY_SUBSTRINGS the manager's full tool set, including these, is
checked against at graph build in ``build_orchestrator_agent``) and by this module's own
curation above.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from orchestrator.agent.accounting_lane import (
    accounting_categorize_books,
    accounting_organize_invoices_expenses,
    accounting_prepare_tax_summary,
    accounting_reconcile_transactions,
)
from orchestrator.agent.cost_opt_lane import (
    analyze_marketing_roi,
    analyze_tenant_spend,
    analyze_unit_economics,
    identify_spend_anomaly,
    read_cost_context,
)
from orchestrator.agent.finance_lane import (
    analyze_cash_flow,
    analyze_receivables,
    pricing_margin_input,
    propose_payment_reminder,
)
from orchestrator.agent.marketing_lane import (
    check_ad_spend_intent,
    check_send_intent,
    draft_campaign_plan,
    draft_content,
    list_recent_campaigns,
)
from orchestrator.agent.sales_lane import (
    identify_repeat_upsell_opportunity,
    recommend_sales_play,
)
from orchestrator.agent.tech_lane import (
    advise_integration_setup,
    check_config_change_intent,
    propose_config_change,
    read_integration_health,
    read_listing_health,
    read_tech_context,
)

# The curated advisory subset — see the module docstring's CLASSIFICATION table for the
# full include/exclude reasoning per tool. Passed as (part of) build_orchestrator_agent's
# extra_tools by supervisor.build_supervisor_graph — the Manager's OWN tool inventory,
# never a sub-graph / handoff / spawn tool.
ADVISORY_TOOLS: list[BaseTool] = [
    # sales
    recommend_sales_play,
    identify_repeat_upsell_opportunity,
    # marketing
    list_recent_campaigns,
    draft_campaign_plan,
    draft_content,
    check_send_intent,
    check_ad_spend_intent,
    # finance
    analyze_cash_flow,
    analyze_receivables,
    pricing_margin_input,
    propose_payment_reminder,
    # accounting
    accounting_categorize_books,
    accounting_prepare_tax_summary,
    accounting_organize_invoices_expenses,
    accounting_reconcile_transactions,
    # tech
    read_integration_health,
    read_listing_health,
    advise_integration_setup,
    read_tech_context,
    propose_config_change,
    check_config_change_intent,
    # cost optimisation
    analyze_tenant_spend,
    analyze_unit_economics,
    identify_spend_anomaly,
    analyze_marketing_roi,
    read_cost_context,
]

__all__ = ["ADVISORY_TOOLS"]
