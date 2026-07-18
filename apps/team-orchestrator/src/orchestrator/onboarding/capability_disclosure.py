"""D2 (Fazal 2026-07-12 decision #2) — honest capability-disclosure for an UNSUPPORTED paid ad-boost.

GROUND TRUTH (decisive): there is NO paid-ad executor anywhere. ``agent/roster.py`` = exactly three
specialists (sales_recovery / integration / onboarding_conductor); marketing is an ADVISORY lane, not
an autonomous ad engine. The manager genuinely CANNOT place a paid Instagram / Facebook / Google ad
boost. Arming an owner approval for an action that cannot execute would be an impossible-promise
(§2.5). So an owner ask to run a PAID ad boost must get an honest capability-limit statement + a pivot
to what IS supported (a WhatsApp win-back to lapsed customers) — never a fabricated "boosted"/"paid",
never a same-turn spend claim, never an armed approval for the unexecutable.

Wired as a sibling net in ``runner.webhook_pipeline_run``'s first-contact block (after the connector
first-contact net), BEFORE the brain / triage / spend-approval path: a paid-boost ask is DISCLOSED and
the run closes — it never enters the spend gate. Money-safe by construction (this net has no send/
effect path; it only speaks). Pure detector + copy, Pillar 1 zero-LLM, FAIL-OPEN (any error -> net does
not fire -> normal path).

NOT a roadmap block: a real paid-ad executor is a FUTURE capability (Fazal's call if ever). For launch,
disclose is correct — do NOT build an executor here.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("orchestrator.onboarding.capability_disclosure")

# PAID external-ad-platform tokens. Deliberately NOT the broad ``_AD_REF_TOKENS`` (ad/ads/promo/
# campaign) — those over-fire on supported win-back campaigns and generic "add"/"promo" text. Boosting
# a post is inherently a paid IG/FB action, so "boost" belongs here; the social/search platform names
# are the other high-signal tokens. "google" stays (a "google sheet" CONNECT ask has no money signal AND
# is caught by the connector first-contact net that runs BEFORE this one).
_PAID_AD_PLATFORM_TOKENS = frozenset(
    {
        "boost", "boosted", "instagram", "insta", "ig", "facebook", "fb", "meta",
        "google", "youtube", "yt", "adwords",
    }
)

# Money-INTENT tokens. A paid-ad ask pairs a platform with a spend intent. We require an EXPLICIT
# money token or a ₹-figure — NOT a bare number alone (a bare "500" false-fires on "got 500 likes").
_PAID_MONEY_TOKENS = frozenset(
    {
        "paid", "pay", "rupaye", "rupaya", "rupees", "rupee", "paisa", "paise",
        "budget", "spend", "kharch", "kharcha", "₹",
    }
)

# Ad-PLACEMENT verbs (EN + Hinglish). A bare amount ("5000 ka") counts as a money signal ONLY when
# paired with one of these AND a platform token — the high-signal combo (mirrors cluster-2b). This
# catches "google ads laga do 5000" WITHOUT re-admitting "my instagram post got 500 likes" (no
# placement verb there). "boost"/"promote" imply paid intent, so they belong here too.
_AD_PLACE_VERB_TOKENS = frozenset(
    {"laga", "lagao", "lagado", "chala", "chalao", "chalado", "run", "place", "boost", "promote"}
)

_DISCLOSURE_EN = (
    "I can't run paid ad boosts (Instagram, Facebook or Google ads) for you yet — placing paid ads "
    "isn't something I can do directly. What I can do is run a WhatsApp win-back campaign to your "
    "lapsed customers to bring them back, at no ad cost. Want me to set that up?"
)
_DISCLOSURE_HI = (
    "मैं अभी आपके लिए पेड ऐड बूस्ट (Instagram, Facebook या Google ads) नहीं चला सकता — पेड ऐड लगाना "
    "मैं सीधे नहीं कर सकता। लेकिन मैं आपके पुराने ग्राहकों को वापस लाने के लिए WhatsApp win-back कैंपेन "
    "चला सकता हूँ, बिना किसी ऐड खर्च के। क्या मैं वो सेट कर दूँ?"
)


def compose_capability_disclosure(*, locale: str = "en") -> str:
    """The honest capability-limit reply for an unsupported paid ad-boost + a pivot to the supported
    win-back. Pure + deterministic. Pillar-7 copy (Fazal final words; wording tweakable)."""
    return _DISCLOSURE_HI if locale == "hi" else _DISCLOSURE_EN


def detect_unsupported_action(body: str) -> bool:
    """True iff ``body`` is a request to run a PAID ad boost (an external-ad-platform token AND a
    money-intent signal) — the one unsupported paid action the roster cannot execute.

    Opt-out / DSR ALWAYS wins first. FAIL-OPEN: any error -> False (net does not fire; normal path).

    VT-681 phase 3 — registry-gated: this net now fires ONLY while the capability registry still
    declares ``marketing.paid_ad_boost`` disabled. The day that capability graduates to live, the
    net auto-retires (no stale hand-rolled decline over a real feature) — the generalization of
    the D2 one-off onto the registry as the single source of capability truth."""
    try:
        if not body or not body.strip():
            return False
        # VT-681: consult the capability registry FIRST — a graduated (non-disabled) ad-boost
        # means there is nothing to disclose; the net retires itself.
        from orchestrator.capability.registry import mode_of

        if mode_of("marketing.paid_ad_boost") != "disabled":
            return False
        # DPDP: opt-out / DSR routing wins over any other interpretation.
        from orchestrator.pre_filter_gate import matches_opt_out_or_dsr

        if matches_opt_out_or_dsr(body):
            return False
        # Reuse the emission-gate tokenizer + regexes (single source of truth; NOT _AD_REF_TOKENS).
        from orchestrator.agent.emission_gate import _BARE_AMOUNT_RE, _RUPEE_FIGURE_RE, _tokenize

        toks = set(_tokenize(body))
        has_platform = bool(toks & _PAID_AD_PLATFORM_TOKENS)
        has_bare_amount_combo = bool(
            _BARE_AMOUNT_RE.search(body) and (toks & _AD_PLACE_VERB_TOKENS)
        )
        has_money = (
            bool(toks & _PAID_MONEY_TOKENS)
            or bool(_RUPEE_FIGURE_RE.search(body))
            or "₹" in body
            or has_bare_amount_combo
        )
        return has_platform and has_money
    except Exception:  # noqa: BLE001 — a detector failure must never block the turn (fail-open)
        logger.warning("D2 detect_unsupported_action failed (fail-open -> False)", exc_info=True)
        return False


__all__ = ["compose_capability_disclosure", "detect_unsupported_action"]
