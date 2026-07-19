"""VT-577 — the agent DATA-NEEDS registry (declarative, zero-LLM, zero-I/O).

CL-2026-07-03 (Fazal, Standing): the paced onboarding offers integrations ONE AT A TIME,
easiest-first, and EACH offer is JUSTIFIED by what a business agent needs to (a) compose a
month plan and (b) EXECUTE it toward revenue. This module is the single source of truth for
that justification: which DATA CLASSES each agent needs to plan vs to execute, which
INTEGRATIONS supply which classes, how hard each integration is (effort — easiest first),
whether it is BUILT today, and the plain owner-facing "where to find the thing" instructions.

FOCUS RULING (Fazal verbatim): "If an agent can come up with a plan of action for the next
month and is able to execute the plan accordingly, thats our objective and moat." So the
registry keys off two thresholds per agent: ``plan_requires`` (enough to compose the plan)
and ``execute_requires`` (enough to actually act) — the paced flow gates the business summary
+ month plan on ``readiness(agent).can_plan`` (data has landed), NEVER on journey completion.

Pure data + pure functions: no DB, no network, no LLM. Safe to import in the dep-less smoke.
The paced completion flow (VT-576, journey.py) consumes ``next_best_integration`` and
``readiness``; the registry never sends or persists anything itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# DATA CLASSES — the kinds of business data an agent consumes. Derived from what
# the BUILT agent actually reads: sales_recovery detects lapsed BUYERS, so it needs
# the customer contact records + the sale-of-record history, and a channel to reach
# them; the declared-future marketing agent retargets dropouts/window-shoppers, so it
# adds web traffic + ad accounts + the product catalog.
# ---------------------------------------------------------------------------

CUSTOMERS_CONTACTABLE = "customers_contactable"
TRANSACTIONS_HISTORY = "transactions_history"
PRODUCT_CATALOG = "product_catalog"
WEB_TRAFFIC = "web_traffic"
AD_ACCOUNTS = "ad_accounts"
MESSAGING_CHANNEL = "messaging_channel"


@dataclass(frozen=True)
class DataClass:
    """One kind of business data + owner-facing labels. ``owner_label`` is the descriptive 'seeing X'
    phrase (why copy); ``need_label`` is the compact noun for the 'I need X first' blocked-reason line."""

    id: str
    owner_label: str
    need_label: str


DATA_CLASSES: dict[str, DataClass] = {
    CUSTOMERS_CONTACTABLE: DataClass(
        CUSTOMERS_CONTACTABLE, "who your customers are and how to reach them", "your customer list"
    ),
    TRANSACTIONS_HISTORY: DataClass(
        TRANSACTIONS_HISTORY, "your past sales, so I can spot who's stopped buying", "your sales history"
    ),
    PRODUCT_CATALOG: DataClass(PRODUCT_CATALOG, "what you sell", "your product catalogue"),
    WEB_TRAFFIC: DataClass(WEB_TRAFFIC, "who's visiting your site but not buying", "your website traffic"),
    AD_ACCOUNTS: DataClass(AD_ACCOUNTS, "your ad accounts, to retarget the right people", "your ad accounts"),
    MESSAGING_CHANNEL: DataClass(
        MESSAGING_CHANNEL, "a WhatsApp channel to actually reach your customers", "a messaging channel"
    ),
}


# ---------------------------------------------------------------------------
# AGENT NEEDS — per business agent: what data it needs to PLAN a month vs to EXECUTE it,
# with a one-line owner-facing "why". ``available_today`` gates whether the agent is offered
# as a live capability; ``sales_recovery`` is the built agent today, ``marketing`` is a
# DECLARED FUTURE entry (its needs shape the registry but it is not offered yet).
# ---------------------------------------------------------------------------

SALES_RECOVERY = "sales_recovery"
MARKETING = "marketing"


@dataclass(frozen=True)
class AgentNeed:
    agent: str
    why: str
    plan_requires: frozenset[str]
    execute_requires: frozenset[str]
    available_today: bool


AGENT_NEEDS: dict[str, AgentNeed] = {
    SALES_RECOVERY: AgentNeed(
        agent=SALES_RECOVERY,
        why="win back buyers who've gone quiet and recover lost revenue",
        # To COMPOSE a win-back plan I need to see who your customers are and their purchase
        # history (to compute who's lapsed). To EXECUTE it I additionally need a channel to send.
        plan_requires=frozenset({CUSTOMERS_CONTACTABLE, TRANSACTIONS_HISTORY}),
        execute_requires=frozenset(
            {CUSTOMERS_CONTACTABLE, TRANSACTIONS_HISTORY, MESSAGING_CHANNEL}
        ),
        available_today=True,
    ),
    MARKETING: AgentNeed(
        agent=MARKETING,
        why="bring back window-shoppers and inactive customers with retargeting",
        plan_requires=frozenset({WEB_TRAFFIC, PRODUCT_CATALOG}),
        execute_requires=frozenset({WEB_TRAFFIC, AD_ACCOUNTS, MESSAGING_CHANNEL}),
        available_today=False,  # declared future — shapes the registry, not offered yet
    ),
}

# The agents offered TODAY, in default priority order (revenue-first). ``next_best_integration``
# ranks against these unless the caller passes an explicit ``agent_priorities``.
DEFAULT_AGENT_PRIORITIES: tuple[str, ...] = (SALES_RECOVERY,)


# ---------------------------------------------------------------------------
# INTEGRATIONS — per integration: which data classes it supplies, an effort score
# (LOWER = easier, offered first), whether it is BUILT today (``available_today``), and
# stepwise owner instructions ("where to find the thing"). Only shopify + google_sheets +
# file_upload are built today; the rest are declared but ``coming_soon`` and are NEVER
# offered as a next step (CL-2026-07-03).
# ---------------------------------------------------------------------------

SHOPIFY = "shopify"
GOOGLE_SHEETS = "google_sheets"
FILE_UPLOAD = "file_upload"
GSC = "gsc"
GA = "ga"
GOOGLE_MERCHANT = "google_merchant"
WABA = "waba"
META_MARKETING = "meta_marketing"
FB_INSTA = "fb_insta"


@dataclass(frozen=True)
class Integration:
    id: str
    label: str
    supplies: frozenset[str]
    effort: int  # lower = easier for the owner; offered first
    available_today: bool
    instructions: str  # plain, stepwise "where to find the thing" copy


INTEGRATIONS: dict[str, Integration] = {
    SHOPIFY: Integration(
        id=SHOPIFY,
        label="Shopify",
        supplies=frozenset({CUSTOMERS_CONTACTABLE, TRANSACTIONS_HISTORY, PRODUCT_CATALOG}),
        effort=1,
        available_today=True,
        instructions=(
            "Your store address looks like yourstore.myshopify.com — find it in your Shopify "
            "admin under Settings → Domains. Reply with it here and I'll send you a secure "
            "link to connect (one tap, nothing to copy-paste)."
        ),
    ),
    GOOGLE_SHEETS: Integration(
        id=GOOGLE_SHEETS,
        label="Google Sheets",
        supplies=frozenset({CUSTOMERS_CONTACTABLE, TRANSACTIONS_HISTORY}),
        effort=2,
        available_today=True,
        instructions=(
            "If your customers or sales are in a Google Sheet, I can read it. Reply 'sheet' and "
            "I'll send a link to connect your Google account — you just pick the sheet, no "
            "copying anything."
        ),
    ),
    FILE_UPLOAD: Integration(
        id=FILE_UPLOAD,
        label="a file upload",
        supplies=frozenset({CUSTOMERS_CONTACTABLE, TRANSACTIONS_HISTORY}),
        effort=3,
        available_today=True,
        instructions=(
            "Have a customer or sales list as a PDF or Excel/CSV? Tap the attach button here in "
            "WhatsApp and send me the file — I'll read it automatically."
        ),
    ),
    # --- declared-but-unbuilt (coming_soon): NEVER offered as a next step ---
    GSC: Integration(
        id=GSC,
        label="Google Search Console",
        supplies=frozenset({WEB_TRAFFIC}),
        effort=4,
        available_today=False,
        instructions="(coming soon) Connect Google Search Console to see what people search to find you.",
    ),
    GA: Integration(
        id=GA,
        label="Google Analytics",
        supplies=frozenset({WEB_TRAFFIC}),
        effort=4,
        available_today=False,
        instructions="(coming soon) Connect Google Analytics to see who visits your site.",
    ),
    GOOGLE_MERCHANT: Integration(
        id=GOOGLE_MERCHANT,
        label="Google Merchant Center",
        supplies=frozenset({PRODUCT_CATALOG}),
        effort=5,
        available_today=False,
        instructions="(coming soon) Connect Google Merchant Center to sync your product catalog.",
    ),
    WABA: Integration(
        id=WABA,
        label="WhatsApp Business",
        supplies=frozenset({MESSAGING_CHANNEL}),
        effort=5,
        available_today=False,
        instructions="(coming soon) Set up a WhatsApp Business sending channel to reach your customers.",
    ),
    META_MARKETING: Integration(
        id=META_MARKETING,
        label="Meta Ads",
        supplies=frozenset({AD_ACCOUNTS}),
        effort=6,
        available_today=False,
        instructions="(coming soon) Connect Meta Ads to retarget the right people.",
    ),
    FB_INSTA: Integration(
        id=FB_INSTA,
        label="Facebook & Instagram",
        supplies=frozenset({WEB_TRAFFIC, AD_ACCOUNTS}),
        effort=6,
        available_today=False,
        instructions="(coming soon) Connect Facebook & Instagram to see engagement and retarget.",
    ),
}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationSuggestion:
    """An ordered next-step suggestion: WHICH integration, WHY (tied to the agent need it
    unlocks), and the stepwise instructions to complete it."""

    integration: str
    label: str
    supplies: frozenset[str]
    effort: int
    why: str
    instructions: str
    unlocks_agents: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Readiness:
    """Whether an agent can PLAN / EXECUTE given the connected set, plus the still-missing
    data classes for each threshold (owner-facing labels resolvable via ``DATA_CLASSES``)."""

    agent: str
    can_plan: bool
    can_execute: bool
    missing_for_plan: frozenset[str]
    missing_for_execute: frozenset[str]


def supplied_classes(connected: set[str]) -> set[str]:
    """Union of the data classes supplied by the connected integrations. Unknown ids are
    ignored (fail-soft); coming_soon integrations still count IF somehow connected (the
    supply is real even when we don't yet OFFER the connector)."""
    out: set[str] = set()
    for cid in connected:
        integ = INTEGRATIONS.get(cid)
        if integ is not None:
            out |= integ.supplies
    return out


def readiness(agent: str, connected: set[str]) -> Readiness:
    """Can ``agent`` PLAN / EXECUTE given the connected integration ids? ``can_plan`` gates the
    business summary + month plan (VT-576); ``can_execute`` gates the agent actually acting."""
    need = AGENT_NEEDS[agent]
    have = supplied_classes(connected)
    missing_plan = need.plan_requires - have
    missing_exec = need.execute_requires - have
    return Readiness(
        agent=agent,
        can_plan=not missing_plan,
        can_execute=not missing_exec,
        missing_for_plan=frozenset(missing_plan),
        missing_for_execute=frozenset(missing_exec),
    )


def _integrations_supplying(classes: frozenset[str] | set[str]) -> list[str]:
    """The available_today integration ids that supply ≥1 of ``classes``, easiest-first. The owner-
    facing 'how to fix it' menu for a blocked plan."""
    want = set(classes)
    fixers = [
        cid for cid, integ in INTEGRATIONS.items()
        if integ.available_today and (integ.supplies & want)
    ]
    fixers.sort(key=lambda cid: (INTEGRATIONS[cid].effort, cid))
    return fixers


def _join_human(items: Sequence[str], *, conj: str = "and") -> str:
    """'a', 'a and b', 'a, b or c' — a plain owner-facing list join."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {conj} {items[-1]}"


def plan_blocked_reason(agent: str, connected: set[str]) -> str | None:
    """Owner-facing one-liner naming what ``agent`` still needs before it can PLAN — the
    'say what's missing instead of emitting a hollow plan' contract (CL-2026-07-03-plan-governance).
    Returns None when the agent CAN plan. This is the SINGLE SOURCE OF TRUTH for the no-plan message:
    the VT-576 defer path renders it, and a VT-578 specialist self-check calls it before composing.

    Importable outside onboarding (pure, dep-less) so specialists + the Team Manager can call it.
    """
    r = readiness(agent, connected)
    if r.can_plan:
        return None
    need = _join_human([DATA_CLASSES[c].need_label for c in sorted(r.missing_for_plan) if c in DATA_CLASSES])
    fixers = _integrations_supplying(r.missing_for_plan)
    how = _join_human([INTEGRATIONS[i].label for i in fixers], conj="or")
    if need and how:
        return f"I need {need} first — connect {how}."
    if need:
        return f"I need {need} first."
    return "I need a bit more of your business data before I can build a plan."


def _needed_classes(agent_priorities: Sequence[str]) -> list[str]:
    """The data classes the priority agents need (plan + execute), de-duped, in priority order —
    the demand side that ranks the supply (integrations)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for agent in agent_priorities:
        need = AGENT_NEEDS.get(agent)
        if need is None:
            continue
        for dc in (*sorted(need.plan_requires), *sorted(need.execute_requires)):
            if dc not in seen:
                seen.add(dc)
                ordered.append(dc)
    return ordered


def next_best_integration(
    connected: set[str],
    agent_priorities: Sequence[str] | None = None,
) -> list[IntegrationSuggestion]:
    """Ordered next-step suggestions: available_today integrations that supply a data class the
    priority agents still need, easiest first. Already-connected integrations and integrations
    that add nothing still-needed are dropped; coming_soon integrations are NEVER offered.

    Ordering (enablement, then effort): by (# still-needed classes supplied) DESC, then
    ``effort`` ASC, then id — so the integration that unblocks the most agent-need with the least
    owner effort comes first. Each suggestion's ``why`` names the agent capability it unlocks.
    """
    priorities = list(agent_priorities) if agent_priorities else list(DEFAULT_AGENT_PRIORITIES)
    needed = set(_needed_classes(priorities))
    have = supplied_classes(connected)
    still_needed = needed - have

    suggestions: list[IntegrationSuggestion] = []
    for cid, integ in INTEGRATIONS.items():
        if cid in connected or not integ.available_today:
            continue
        contributes = integ.supplies & still_needed
        if not contributes:
            continue
        unlocks = tuple(
            a for a in priorities if AGENT_NEEDS.get(a) and (AGENT_NEEDS[a].plan_requires & contributes)
        )
        suggestions.append(
            IntegrationSuggestion(
                integration=cid,
                label=integ.label,
                supplies=integ.supplies,
                effort=integ.effort,
                why=_compose_why(contributes, unlocks or tuple(priorities)),
                instructions=integ.instructions,
                unlocks_agents=unlocks,
            )
        )

    suggestions.sort(key=lambda s: (-len(s.supplies & still_needed), s.effort, s.integration))
    return suggestions


def _compose_why(contributes: set[str], agents: Sequence[str]) -> str:
    """Owner-facing justification: the agent goal + what this connection lets me see. Never a
    citation marker, never jargon — one plain sentence."""
    goal = AGENT_NEEDS[agents[0]].why if agents and agents[0] in AGENT_NEEDS else "grow your revenue"
    labels = [DATA_CLASSES[c].owner_label for c in sorted(contributes) if c in DATA_CLASSES]
    if labels:
        seeing = labels[0] if len(labels) == 1 else " and ".join([", ".join(labels[:-1]), labels[-1]])
        return f"This lets me {goal} — I'll be able to see {seeing}."
    return f"This lets me {goal}."


__all__ = [
    "DATA_CLASSES",
    "AGENT_NEEDS",
    "INTEGRATIONS",
    "DEFAULT_AGENT_PRIORITIES",
    "DataClass",
    "AgentNeed",
    "Integration",
    "IntegrationSuggestion",
    "Readiness",
    "supplied_classes",
    "readiness",
    "plan_blocked_reason",
    "next_best_integration",
    # agent ids
    "SALES_RECOVERY",
    "MARKETING",
    # integration ids
    "SHOPIFY",
    "GOOGLE_SHEETS",
    "FILE_UPLOAD",
    # data class ids
    "CUSTOMERS_CONTACTABLE",
    "TRANSACTIONS_HISTORY",
    "PRODUCT_CATALOG",
    "WEB_TRAFFIC",
    "AD_ACCOUNTS",
    "MESSAGING_CHANNEL",
]
