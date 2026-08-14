"""VT-740 — per-recipient send suppression. VT-741 supplies the number.

WHAT THIS DEFENDS AGAINST
-------------------------
At least three paths automatically re-drive a manager_task, and none of them asks what already went
out: the hourly reaper wake (`orphan_reaper`, effect-blind, and the same code is on `main`),
`approval_resume.redrive_task` when an owner replies yes, and an operator redrive. A task whose
campaign messaged 40 of 100 customers and then died is re-driven on exactly the same terms as one
that sent nothing.

Send idempotency does not cover it: `idempotency_key = f"agent:{draft_id}"` is keyed to the DRAFT,
and a re-drive re-runs the specialist, which mints new drafts and therefore new keys.

WHY IT LIVES AT THE SEND CHOKE AND NOT ON THE RE-DRIVE PATHS
------------------------------------------------------------
Gating the re-drive paths one at a time means getting all of them right, forever, including the
ones nobody has thought of. A guard at the send chokepoint is path-independent: it does not care
WHY a second send was attempted, only that this customer already received one. It also needs no
task->campaign attribution, which is the thing that does not currently work
(`campaign_messages.campaign_id` is never populated).

THE (a)/(b) LINE — carried from Fazal's 2026-08-10 direction, deliberately, into the code
-----------------------------------------------------------------------------------------
**(a) "How often should THIS business contact this customer?"** is a genuine product/policy call.
It is tenant-specific, it may one day be reasoned by the Manager rather than tabulated, and it is
what `resolve_interval_hours` below is the socket for.

**(b) "Should this customer receive the same message twice because a process crashed?"** is NOT a
policy question. There is no tenant for whom the answer is yes, and asking the Manager to have an
opinion about a bug is a category error. That half is this module's fixed behaviour and must never
become configurable.

RELATIONSHIP TO THE AGENT-CONTACT CAPS (Clau's audit question, answered here)
-----------------------------------------------------------------------------
`agents/customer_send.py` already carries `RECONTACT_SUPPRESSION_DAYS = 30`,
`MAX_AGENT_CONTACTS_PER_90D = 2`, `AGENT_SEND_CUSTOMER_WEEKLY_CAP` and a tenant daily cap. **Those
are NOT redundant and are NOT retired by this module.** They answer a different question and read a
different table:

- **The agent caps** ask *"how often may an AGENT cold-contact this customer?"* They read
  `agent_customer_contacts` and bind the agent draft-send path only. They are the stricter,
  narrower bar — a 2-per-90-days ceiling on unsolicited outreach.
- **This module** asks *"has this customer been delivered ANY message recently?"* It reads the send
  ledger and binds every customer send, campaign fan-out included.

**Precedence is deterministic without needing a rule, because both are VETO-ONLY.** Neither can
authorize a send; each can only stop one. Two conjunctive vetoes compose to "most restrictive
wins" by construction, and the outcome is identical whichever runs first — so call order is not a
tie-break that could drift. That is the property to preserve: if either layer is ever given a
branch that PERMITS a send, this ceases to be true and the two become a genuine conflict.

SUPPRESSION ONLY, NEVER AUTHORIZATION
-------------------------------------
Nothing here can permit a send. Consent, opt-out, complaint-freeze, onboarded/activation,
ownership and Pillar-7 all sit UPSTREAM and are untouched. Every failure mode in this module
suppresses harder, never softer: an unreadable ledger, a missing customer, a DB error and an empty
history all resolve to the most conservative interval.

VT-741 — THE TIERS ARE RECENCY, NOT POSITION (Fazal, re-specified 2026-08-13)
-----------------------------------------------------------------------------
An earlier drafting of this rule expressed the middle tier POSITIONALLY ("read or clicked or
replied in the last 10 messages"). That was a drafting error and is dead. The rule is recency:

    Tier A: replied or clicked within 30 days        -> 24h
    Tier B: read / clicked / replied within 90 days  -> 3 days
    Tier C: everyone else                            -> 7 days

Recency is also the only form the substrate can actually answer honestly. The reply signal exists
ONLY as ``wa_conversations.last_inbound_at`` — one timestamp per (tenant, phone_token), keyed on
the PHONE TOKEN and not on ``customer_id``, with no per-message inbound history anywhere. A
positional rule ("in the last 10 messages") is therefore not merely inconvenient to compute, it is
unanswerable without inventing a message-level inbound capture table. That capture is VT-744, and
is deliberately NOT built here.

FIRST MATCH WINS, AND A IS EVALUATED BEFORE B — THIS IS LOAD-BEARING
--------------------------------------------------------------------
Tier A is a STRICT SUBSET of Tier B: every customer who replied or clicked inside 30 days also
replied or clicked inside 90 days, so they satisfy B as well. Evaluate B first and the check
"matches B?" succeeds for the whole of A, silently capping the single most engaged cohort at the
middle interval. Nothing errors; the tier is simply always wrong for exactly the customers the
rule was written for. ``_TIER_ORDER`` below is the ordering, ``_assert_tier_order_invariants()``
enforces it at import, and the ordering is asserted behaviourally in the tests — an ordering that
only lives in a comment is one refactor from being lost.

WHY EVERY FAILURE IS TIER C AND NOT TIER A
-------------------------------------------
Tier C is the LONGEST interval, so resolving to it suppresses the most. A missing signal, an
unreadable table, a customer with no phone and a customer with no history are all indistinguishable
from "not engaged", and treating them as engaged would shorten the interval on exactly the
customers we know least about. Note the direction carefully: shortening an interval never
AUTHORIZES a send (every upstream gate still binds), it only narrows this layer's veto — but it
narrows it on no evidence, which is the wrong default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

logger = logging.getLogger("orchestrator.agents.send_frequency")

#: The fail-closed interval, in hours. This is NOT a number invented here: Fazal ratified the
#: frequency rule on 2026-08-10 with Tier C ("everyone else", 7 days) as the explicit fail-closed
#: floor — "error, partial read, missing data, zero history -> Tier C". It stays the fail-closed
#: value under the VT-741 re-spec: it is the LONGEST interval, so it suppresses the most.
FAIL_CLOSED_INTERVAL_HOURS = 7 * 24

#: `send_idempotency_keys.send_status` values that mean a message actually REACHED the customer.
#: The others ('window_closed', 'rate_limited', 'error') are recorded ATTEMPTS that did not
#: deliver — counting them would suppress a customer who never heard from us.
_DELIVERED = ("sent",)

#: The customer-attributed click table (VT-741, migration 201). Named as a literal rather than
#: imported from ``integrations.hook_links`` ON PURPOSE: this module is imported by the dep-less
#: test suite and ``hook_links`` pulls in ``orchestrator.graph`` (psycopg). The static test
#: ``test_the_click_table_name_matches_the_migration_and_hook_links`` is what keeps the literal
#: honest — it fails if the migration or hook_links ever renames the table.
_CLICK_TABLE = "customer_hook_links"


@dataclass(frozen=True, slots=True)
class EngagementSignals:
    """How long ago, in days, each engagement signal last fired. ``None`` = never / unknown.

    Ages are computed BY POSTGRES against its own ``now()``, not against the orchestrator's clock,
    for the same reason ``recent_delivery_within`` does: the send path and the ledger must not
    disagree about what "30 days ago" means because a container's clock drifted.
    """

    replied_age_days: float | None = None
    clicked_age_days: float | None = None
    read_age_days: float | None = None


@dataclass(frozen=True, slots=True)
class Tier:
    """One rung of the ratified rule. ``window_days=None`` is the catch-all ("everyone else")."""

    name: str
    interval_hours: int
    window_days: int | None
    #: Which ``EngagementSignals`` fields count for this tier. Note Tier A deliberately omits
    #: ``read``: a read is a weaker signal than a reply or a click (it is passive, and Twilio
    #: reports it for a message the customer may have dismissed), so it earns the middle tier
    #: only. That asymmetry is the whole reason A and B are different tiers rather than two
    #: windows on one predicate.
    signals: tuple[str, ...]

    def matches(self, signals: EngagementSignals) -> bool:
        if self.window_days is None:
            return True  # the catch-all rung; see FAIL_CLOSED_INTERVAL_HOURS
        for name in self.signals:
            age = getattr(signals, f"{name}_age_days")
            if age is not None and 0 <= age <= self.window_days:
                return True
        return False


TIER_A = Tier(name="A", interval_hours=24, window_days=30, signals=("replied", "clicked"))
TIER_B = Tier(name="B", interval_hours=3 * 24, window_days=90,
              signals=("read", "clicked", "replied"))
TIER_C = Tier(name="C", interval_hours=FAIL_CLOSED_INTERVAL_HOURS, window_days=None, signals=())

#: **Order is the rule, not a detail.** A before B before C; first match wins. See the module
#: docstring — A is a strict subset of B, so a swapped order is a silent, error-free wrong answer
#: for the most engaged cohort.
_TIER_ORDER: tuple[Tier, ...] = (TIER_A, TIER_B, TIER_C)


def _assert_tier_order_invariants() -> None:
    """Fail at IMPORT, not at send time, if the tier table is edited into an unsafe shape.

    Three properties, each one a real failure this guards:

    1. **Ascending windows, A before B.** The subset relation is what makes order load-bearing.
    2. **Ascending intervals, catch-all last.** The catch-all must be the LONGEST interval or the
       fail-closed direction inverts and an unknown customer starts getting the engaged cadence.
    3. **Every interval positive.** An interval of 0 turns ``recent_delivery_within`` into a query
       over an empty window, which never matches, which makes this layer a silent no-op. That is
       the one edit that could genuinely increase send rate, so it is the one this refuses to load
       with. Importing this module is inside the caller's try/except, so a raise here suppresses
       sends rather than permitting them.
    """
    ranked = _TIER_ORDER[:-1]
    catch_all = _TIER_ORDER[-1]
    if catch_all.window_days is not None:
        raise RuntimeError("send_frequency: the LAST tier must be the catch-all (window_days=None)")
    if any(t.window_days is None for t in ranked):
        raise RuntimeError("send_frequency: only the last tier may be a catch-all")
    windows = [t.window_days for t in ranked]
    if windows != sorted(windows):  # type: ignore[type-var]
        raise RuntimeError(f"send_frequency: tier windows must ascend (A before B), got {windows}")
    intervals = [t.interval_hours for t in _TIER_ORDER]
    if intervals != sorted(intervals):
        raise RuntimeError(f"send_frequency: tier intervals must ascend, got {intervals}")
    if min(intervals) <= 0:
        raise RuntimeError("send_frequency: a non-positive interval would disable the veto")
    if catch_all.interval_hours != FAIL_CLOSED_INTERVAL_HOURS:
        raise RuntimeError(
            "send_frequency: the catch-all interval must BE the fail-closed interval, so the "
            "'everyone else' path and the 'we could not tell' path cannot drift apart"
        )


_assert_tier_order_invariants()


#: One round trip for all three signals. Each is an independent scalar sub-select, so a customer
#: with (say) no wa_conversations row still gets the click + read answers instead of no row at all.
#: ``EXTRACT(EPOCH ...)`` of a NULL max() is NULL, which reads back as "no signal".
#:
#: **THE CLICK SIGNAL IS NOT FED YET (VT-745). Do not read a Tier A/B result as "clicked".**
#: ``customer_hook_links`` has a mint function (``hook_links.mint_customer_hook_link``) and a
#: recorder, but NOTHING CALLS THE MINT — the only mint surface,
#: ``POST /api/orchestrator/hooks/mint``, has no customer variant. So the table is empty in
#: production and ``clicked_age_s`` is NULL on every call. The tiers therefore currently evaluate:
#:
#:     Tier A -> "replied within 30 days"            (not replied-or-clicked)
#:     Tier B -> "read or replied within 90 days"    (not read-or-clicked-or-replied)
#:
#: That degradation is SAFE — a missing signal can only push a customer toward Tier C, the longest
#: interval and the most suppression — but it is NOT the rule Fazal ratified, so it must not be
#: reported as if it were. Any analysis of tier distribution is analysing two signals, not three.
#:
#: **VT-745 INVESTIGATED 2026-08-14: "wire the mint into the send path" IS NOT AVAILABLE, and the
#: reason is deeper than a missing caller.** Two independent findings:
#:
#:   1. **No customer-audience template can carry a link.** Every customer template in
#:      ``config/twilio_templates.yaml`` (``team_winback_simple``, ``team_winback_offer``,
#:      ``team_opt_out_confirmation``, …) declares only text positionals — none has a URL/link
#:      variable. The one link-bearing template, ``trial_subscribe_link``, is audience=owner. So
#:      there is nowhere in an approved customer message to PUT a tracked link. Free-form is not an
#:      escape: it needs an open 24h window, and a win-back targets lapsed customers by definition.
#:   2. **A hook link would not make sense in a WhatsApp message anyway.** ``GET /r/{token}``
#:      redirects to the tenant's ``wa.me`` (``api/hook_links.py:68``) — it exists to pull someone
#:      from OUTSIDE WhatsApp into a chat. Sending it to a customer already in WhatsApp redirects
#:      them to where they already are.
#:
#: So ``clicked`` is not "unwired", it is **unobtainable through any channel this product has**.
#: Closing it needs a Fazal/Meta decision — either a new customer template carrying a URL variable,
#: or amending the ratified rule to the two signals that exist. Until one of those happens this note
#: IS the deliverable, and ``test_vt745_click_signal_honesty.py`` fails the moment reality changes and
#: this text does not (a production mint caller appears, or a customer template gains a link
#: variable), so the note cannot rot into the next stale-status defect.
#:
#: This is the fifth thing this week found built, exported and called by nothing (the O8 retrieval
#: engine, ``prod_workflow_diagnosis``, two reverted wake gates). Writing the shortfall down here,
#: at the point of the read, is the cheapest defence against the sixth.
_ENGAGEMENT_SQL = f"""
SELECT
    EXTRACT(EPOCH FROM (now() - (
        SELECT max(w.last_inbound_at) FROM wa_conversations w
         WHERE w.tenant_id = %s AND w.phone_token = %s
    ))) AS replied_age_s,
    EXTRACT(EPOCH FROM (now() - (
        SELECT max(k.last_clicked_at) FROM {_CLICK_TABLE} k
         WHERE k.tenant_id = %s AND k.customer_id = %s
    ))) AS clicked_age_s,
    EXTRACT(EPOCH FROM (now() - (
        SELECT max(COALESCE(a.delivery_updated_at, a.sent_at)) FROM agent_customer_contacts a
         WHERE a.tenant_id = %s AND a.customer_id = %s AND a.delivery_status = 'read'
    ))) AS read_age_s
"""  # noqa: S608 — _CLICK_TABLE is a module constant, never user input

_SECONDS_PER_DAY = 86400.0


def _col(row: Any, key: str, index: int) -> Any:
    """Read one column from a psycopg row that may be a dict row or a tuple row."""
    if isinstance(row, dict):
        return row.get(key)
    return row[index]


def _phone_token_for_phone(phone_e164: str | None) -> str | None:
    """The customer's ``wa_conversations.phone_token`` from a phone the CALLER already holds.

    The phone is a parameter rather than a lookup for two reasons, both found by review:

    1. **VT-72.** Reading ``customers`` here was direct access to a tenant-scoped hot table outside
       the wrapper layer, and the no-direct-tenant-db-access gate correctly refused it.
    2. **Contention.** The one caller (``send_whatsapp_template``) already has ``phone_e164`` in
       hand from its own customer resolve, roughly 25 lines above the frequency gate. Re-reading it
       added a round trip per recipient to a path that previously issued none, against a pool
       capped near the Supavisor client limit — 10k avoidable statements on a 5000-recipient
       campaign, bought for nothing.

    The hashing itself still has to happen in Python: ``wa_conversations`` is keyed on
    ``hash_phone(phone_e164)``, a SALTED SHA-256 whose salt lives in ``TEAM_PHONE_HASH_SALT``, an
    application secret. Computing it in SQL would put that salt in a query string, and any other
    derivation simply would not match the token the inbound path wrote.

    Returns None — never raises — on a missing phone or an unset salt. None costs only the reply
    signal, which pushes toward Tier C: more suppression.
    """
    if not phone_e164:
        return None
    try:
        from orchestrator.utils.phone_token import hash_phone

        return hash_phone(str(phone_e164))
    except Exception:  # noqa: BLE001 — no reply signal is a valid, conservative answer
        logger.warning(
            "VT-741 phone-token hashing failed — the reply signal will be treated as absent "
            "(pushes toward Tier C, i.e. more suppression)", exc_info=True,
        )
        return None


def read_engagement_signals(
    tenant_id: UUID | str, customer_id: UUID | str, *, conn: Any,
    phone_e164: str | None = None,
) -> EngagementSignals | None:
    """The three recency signals for one customer, or **None when they could not be read**.

    None means "could not tell", which the caller resolves to Tier C. It is deliberately distinct
    from ``EngagementSignals()`` (all-None ages), which means "read fine, this customer has no
    history" — both land on Tier C today, but conflating them would hide a broken read behind a
    plausible-looking answer.
    """
    token = _phone_token_for_phone(phone_e164)
    tid, cid = str(tenant_id), str(customer_id)
    try:
        row = conn.execute(
            _ENGAGEMENT_SQL, (tid, token, tid, cid, tid, cid),
        ).fetchone()
    except Exception:  # noqa: BLE001 — an unanswerable question is not "not engaged"
        logger.warning(
            "VT-741 engagement read FAILED tenant=%s customer=%s — falling back to the "
            "fail-closed tier", tenant_id, customer_id, exc_info=True,
        )
        return None
    if row is None:
        return None
    try:
        return EngagementSignals(
            replied_age_days=_age_days(_col(row, "replied_age_s", 0)),
            clicked_age_days=_age_days(_col(row, "clicked_age_s", 1)),
            read_age_days=_age_days(_col(row, "read_age_s", 2)),
        )
    except Exception:  # noqa: BLE001 — a malformed row is a failed read, not an empty history
        logger.warning(
            "VT-741 engagement row unreadable tenant=%s customer=%s — falling back to the "
            "fail-closed tier", tenant_id, customer_id, exc_info=True,
        )
        return None


def _age_days(seconds: Any) -> float | None:
    """Seconds-since-signal (Postgres returns Decimal) -> days. NULL / unparseable -> None."""
    if seconds is None:
        return None
    return float(seconds) / _SECONDS_PER_DAY


def resolve_tier(
    tenant_id: UUID | str, customer_id: UUID | str, *, conn: Any = None,
    phone_e164: str | None = None,
) -> Tier:
    """Which rung of the ratified rule this customer sits on. **First match wins, A before B.**

    ``conn=None`` is not an error condition to work around — it is a caller with no database, and
    a tier cannot be determined without one, so it resolves to the catch-all like every other
    unanswerable case.
    """
    if conn is None:
        return TIER_C
    signals = read_engagement_signals(
        tenant_id, customer_id, conn=conn, phone_e164=phone_e164
    )
    if signals is None:
        return TIER_C
    for tier in _TIER_ORDER:
        if tier.matches(signals):
            return tier
    return TIER_C  # unreachable while the catch-all is last; kept so the function is total


def resolve_interval_hours(
    tenant_id: UUID | str, customer_id: UUID | str, *, conn: Any = None
, phone_e164: str | None = None) -> int:
    """The minimum hours that must pass before this customer may be messaged again.

    **This is the socket**, and it stays the socket. VT-741 replaced its body with the ordered
    recency rule (A 24h / B 3 days / C 7 days, first match wins); if the Manager ever earns the
    call, it gets replaced again and the enforcement below still does not move. That is what the
    socket buys — nothing downstream changes when the number's source does.

    Whatever supplies it, two properties are structural and not negotiable: it can only ever
    SUPPRESS (no return value authorizes a send that another gate refused), and any failure to
    determine a tier resolves to the most conservative interval rather than the most permissive.
    """
    return resolve_tier(
        tenant_id, customer_id, conn=conn, phone_e164=phone_e164
    ).interval_hours


def recent_delivery_within(
    tenant_id: UUID | str,
    customer_id: UUID | str,
    *,
    hours: int,
    conn: Any,
) -> bool | None:
    """Has this customer been DELIVERED a message in the last ``hours``?

    Returns True/False, or **None when the question could not be answered** — which the caller must
    treat as True. Returning False on an error would turn a database blip into a duplicate message
    to a real person, which is precisely the failure this module exists to prevent.

    Reads ``send_idempotency_keys``, which carries ``customer_id`` and is indexed on
    ``(tenant_id, customer_id, created_at)`` — the index `idx_send_idem_tenant_customer_created`,
    created for a per-customer frequency lookup that was never written until now.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM send_idempotency_keys "
            " WHERE tenant_id = %s AND customer_id = %s "
            "   AND send_status = ANY(%s) "
            "   AND created_at >= now() - make_interval(hours => %s) "
            " LIMIT 1",
            (str(tenant_id), str(customer_id), list(_DELIVERED), int(hours)),
        ).fetchone()
    except Exception:  # noqa: BLE001 — an unanswerable question is not a "no"
        logger.warning(
            "VT-740 frequency read FAILED tenant=%s customer=%s — suppressing (fail-closed); "
            "a read error must never become a duplicate send",
            tenant_id, customer_id, exc_info=True,
        )
        return None
    return row is not None


def is_suppressed(
    tenant_id: UUID | str, customer_id: UUID | str, *, conn: Any
, phone_e164: str | None = None) -> tuple[bool, str]:
    """``(suppressed, reason)`` for one customer-bound send.

    ``reason`` is a stable machine code for the caller's envelope + the audit line, never prose
    assembled at the call site. The reason strings are UNCHANGED by VT-741 (callers and the audit
    grep for these prefixes); the tier goes to the log line, where new information belongs.
    """
    tier = resolve_tier(tenant_id, customer_id, conn=conn, phone_e164=phone_e164)
    hours = tier.interval_hours
    recent = recent_delivery_within(tenant_id, customer_id, hours=hours, conn=conn)
    if recent is None:
        return True, f"frequency_check_unavailable:{hours}h"
    if recent:
        logger.info(
            "VT-741 frequency tier=%s interval=%sh tenant=%s customer=%s -> suppressed",
            tier.name, hours, tenant_id, customer_id,
        )
        return True, f"recent_delivery_within:{hours}h"
    return False, ""


__all__ = [
    "FAIL_CLOSED_INTERVAL_HOURS",
    "TIER_A",
    "TIER_B",
    "TIER_C",
    "EngagementSignals",
    "Tier",
    "is_suppressed",
    "read_engagement_signals",
    "recent_delivery_within",
    "resolve_interval_hours",
    "resolve_tier",
]
