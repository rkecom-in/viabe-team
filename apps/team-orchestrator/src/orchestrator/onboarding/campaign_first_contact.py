"""D3 (subsumes cluster-5b) — the deterministic CAMPAIGN first-contact net.

A clear "run a win-back campaign" imperative must be routed DETERMINISTICALLY, not left to
the intermittent triage classifier (the delegation-lane variance root: the SAME ask drew
new_task on one run and legacy on the next — VT-633). Two honest outcomes, both deterministic:

  * EMPTY cohort (the tenant has NO customer-sales data at all) -> we CANNOT run a win-back
    (there is literally no one to recover), so we say so and name the fix (connect data). This
    kills the fabrication class where the manager claims "I've started a win-back to your lapsed
    customers" against a tenant whose customer ledger is empty.
  * HAS sales -> mint a sales_recovery specialist_dispatch plan + start the durable workflow, so
    the win-back actually RUNS (the loop's approval/consent/opt-out rails still gate every send —
    this net changes ROUTING, never the money gates).

This module holds the PURE deciders (detector + cohort read + copy). The plan mint + workflow
start live in ``triage_seam`` (the only enforce-mode caller). Pillar 1: zero LLM. FAIL-OPEN
everywhere: any detector/read error -> the net simply does not fire and the turn falls through
to the normal triage path (never blocks the owner).
"""

from __future__ import annotations

import logging
import re
from uuid import UUID

logger = logging.getLogger("orchestrator.onboarding.campaign_first_contact")

# The honest terminal when the owner asks for a win-back but there is no cohort to recover. Names
# the concrete fix (share/connect sales data) so it is actionable, never a dead end. Pillar-7 copy.
EMPTY_COHORT_REPLY = (
    "I'd run a win-back campaign for you, but I don't have any of your customer sales data yet — "
    "so there's no one to reach out to. Share your customer sales (connect your Google Sheet or "
    "Shopify) and I'll build the win-back list and get started."
)

# VERB ∧ NOUN — BOTH must be present for a campaign IMPERATIVE (tight, low false-positive). A bare
# noun ("how many lapsed customers?") is a status QUERY, not a request to run one; a bare verb
# ("run a report") is not a campaign. EN + Hinglish. Word-boundary anchored; case-insensitive.
# Bare "do" is deliberately EXCLUDED — it is ambiguous (imperative "do a campaign" vs the far more
# common interrogative aux "do I have…"). The strong imperatives below cover the real phrasings; a
# rare "do a campaign" safely falls through to the brain rather than risk hijacking a question.
# PLANNING verbs (make/plan/prepare/put together/draw up + "plan karo") are included — "make me a
# plan to win back my lapsed customers" / "plan a win-back campaign" are the most common phrasings
# and were the delegation-lane stall root (D3 couldn't fire without a matching verb). The VERB∧NOUN
# requirement keeps them tight: "make it faster"/"plan my day" carry no campaign NOUN so never fire.
_CAMPAIGN_VERB_RE = re.compile(
    r"\b(run|start|launch|send|create|build|make|draft|plan|prepare|kick\s*off|set\s*up|reach\s*out|"
    r"put\s*together|draw\s*up|chala(?:o|do|\s*do)?|bhej(?:o|do|\s*do)?|shuru\s*kar(?:o|do)?|"
    r"bana(?:o|do|\s*do)?|plan\s*kar(?:o|do)?)\b",
    re.IGNORECASE,
)
_CAMPAIGN_NOUN_RE = re.compile(
    r"\b(campaign|win[\s-]*back|winback|re[\s-]*engage(?:ment)?|re[\s-]*activation|"
    r"outreach|lapsed|dormant)\b",
    re.IGNORECASE,
)

# Ad-hijack guard: an EXTERNAL paid-ad ask ("run a Facebook ad campaign for me") carries an
# ad-platform token + the generic "campaign" noun, but it is NOT a win-back — it must fall through
# to the brain (which offers to draft the ad copy), never get the win-back no-data reply (a
# non-sequitur). So when an ad-platform token is present, the net fires ONLY if a genuine RECOVERY
# noun is also present (win-back / lapsed / dormant / re-engage / re-activation) — the bare
# "campaign"/"outreach" noun no longer qualifies. A real win-back ("run a win-back campaign for my
# lapsed customers") has no ad-platform token, so it is unaffected.
_AD_PLATFORM_TOKEN_RE = re.compile(
    r"\b(facebook|fb|instagram|insta|ig|meta|google|youtube|yt)\b", re.IGNORECASE
)
_RECOVERY_NOUN_RE = re.compile(
    r"\b(win[\s-]*back|winback|re[\s-]*engage(?:ment)?|re[\s-]*activation|lapsed|dormant)\b",
    re.IGNORECASE,
)

# An imperative is a COMMAND, not a QUESTION. A status/how-to question ("how many lapsed customers
# do I have?", "did you send the campaign?") often carries the same VERB∧NOUN tokens but must NOT
# trigger the net — it routes to the brain to be ANSWERED, not dispatched. A leading interrogative
# word (EN + Hinglish) or a trailing "?" marks a question. Questions fall through safely.
_INTERROGATIVE_LEAD_RE = re.compile(
    r"^\s*(how|what|when|where|why|who|which|whose|is|are|am|do|does|did|can|could|will|would|"
    r"should|kya|kaun|kab|kaise|kahan|kitne|kitni|kitna)\b",
    re.IGNORECASE,
)

# R7 — a POLITE-REQUEST form ("can you draft a win-back plan for my customers?") is an imperative
# dressed as a question: a can/could/will/would-you (or "please") lead + VERB∧NOUN + a FIRST-PERSON
# BENEFICIARY ("for me" / "my customers" / "mujhe"). It IS a command to dispatch, so the trailing "?"
# / interrogative-lead question-rejection must NOT apply. The beneficiary is load-bearing: it keeps a
# bare CAPABILITY question ("can you run campaigns?" — no beneficiary) falling to the brain.
_POLITE_REQUEST_LEAD_RE = re.compile(r"^\s*(can|could|would|will)\s+(you|u)\b", re.IGNORECASE)
_FIRST_PERSON_BENEFICIARY_RE = re.compile(
    r"\b(my|me|mine|mera|meri|mere|mujhe|mujhko|hamare|hamari|humare|apne)\b|for\s+me",
    re.IGNORECASE,
)


def _is_polite_request_form(text: str) -> bool:
    """R7 — True iff ``text`` is a polite-REQUEST imperative (polite lead / 'please' + a first-person
    beneficiary). Callers apply this ONLY after VERB∧NOUN already matched, so it just decides whether
    a question-shaped VERB∧NOUN message is a genuine dispatch request vs a capability question."""
    has_polite_lead = bool(_POLITE_REQUEST_LEAD_RE.match(text)) or "please" in text.lower()
    return has_polite_lead and bool(_FIRST_PERSON_BENEFICIARY_RE.search(text))


def is_campaign_plan_imperative(text: str) -> bool:
    """True iff ``text`` is a deterministic "run a win-back campaign" IMPERATIVE (VERB ∧ NOUN).

    Opt-out / DSR ALWAYS wins first (a "stop"/"delete my data" is never a campaign ask). A plain
    question (leading interrogative or trailing "?") is a status/how-to ask, not a command — it falls
    through to the brain — EXCEPT a POLITE-REQUEST form ("can you draft a win-back plan for my
    customers?"), which is an imperative and DOES dispatch (R7). FAIL-OPEN: any error -> False (the
    net does not fire; normal path handles it)."""
    try:
        if not text or not text.strip():
            return False
        # DPDP: opt-out / DSR routing wins over any other interpretation — never read a STOP /
        # erasure as a request to run a campaign.
        from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

        if matches_opt_out_or_dsr(text):
            return False
        if not (_CAMPAIGN_VERB_RE.search(text) and _CAMPAIGN_NOUN_RE.search(text)):
            return False
        # Ad-hijack guard: an external paid-ad ask ("run a Facebook ad campaign") matches VERB∧NOUN
        # on the generic "campaign", but it is NOT a win-back — fall through to the brain unless a
        # genuine recovery noun (win-back/lapsed/dormant/re-engage) is present.
        if _AD_PLATFORM_TOKEN_RE.search(text) and not _RECOVERY_NOUN_RE.search(text):
            return False
        # A plain question is not an imperative — EXCEPT the polite-request form, which dispatches.
        is_question = text.strip().endswith("?") or bool(_INTERROGATIVE_LEAD_RE.match(text))
        if is_question and not _is_polite_request_form(text):
            return False
        return True
    except Exception:  # noqa: BLE001 — a detector failure must never block the turn (fail-open)
        logger.warning("D3 is_campaign_plan_imperative failed (fail-open -> False)", exc_info=True)
        return False


def campaign_cohort_is_empty(tenant_id: UUID | str) -> bool:
    """True iff the tenant has NO customer with any 'sale' ledger entry — i.e. there is literally
    no base to win back (an EMPTY customer ledger, distinct from "0 lapsed of N"). Reads the SAME
    ``count_with_sales`` truth the lapsed-count answer uses (VT-632), so the "no one to reach out
    to" claim is grounded in the exact set a campaign would target.

    FAIL-OPEN: on any read error, return False — treat as "cohort might exist" so the net does NOT
    emit the empty-cohort message on a transient DB blip; the has-sales/dispatch path or the normal
    triage path handles it instead (never a false "you have no data" against a real cohort)."""
    try:
        from orchestrator.db.wrappers import CustomersWrapper

        return CustomersWrapper().count_with_sales(tenant_id) == 0
    except Exception:  # noqa: BLE001 — a cohort-read failure must never fabricate "no data"
        logger.warning(
            "D3 campaign_cohort_is_empty read failed tenant=%s (fail-open -> False)",
            tenant_id, exc_info=True,
        )
        return False


__all__ = ["EMPTY_COHORT_REPLY", "campaign_cohort_is_empty", "is_campaign_plan_imperative"]
