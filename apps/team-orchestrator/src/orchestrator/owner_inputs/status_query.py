"""VT-84 — owner status-query handler (DETERMINISTIC; NEVER the agent).

The owner asks a fact about THEIR OWN data; we answer with a templated SQL aggregation.
Query-type parse is keyword-based, VT-329-safe (NFC + whitespace/punct split, no
Devanagari-dead `\\b`). Unknown queries fall back to the portal link.

# NEEDS-FAZAL: the response copy (Pillar 7 — owner-facing words) is placeholder; Fazal
reviews wording later. The LOGIC (which SQL, which number) is what lands now.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal
from uuid import UUID

from orchestrator import keyword_match as _km
from orchestrator.db.wrappers import LAPSED_WINDOW_DAYS

StatusQueryType = Literal[
    "customer_count", "lapsed_count", "lapsed_list", "top_spend", "last_campaign", "opt_out_count",
    "billing", "unknown",
]

_DASHBOARD = "https://viabe.ai/team/dashboard"

# VT-632 — Fazal's canonical customer-facing definition (2026-07-09; unified CL-2026-07-10): a
# LAPSED / dormant customer is one with NO purchase in the last ``LAPSED_WINDOW_DAYS`` days. The
# constant is defined in ``db.wrappers`` (imported above) as the SINGLE SOURCE OF TRUTH — the only
# runtime lapsed-window value; reference it, never re-literal 45. Since CL-2026-07-10 (option 2) the
# Sales-Recovery SEND cohort uses this SAME window (no longer the VT-312 percentile), so the number
# the owner hears here IS the exact set a win-back campaign targets.

# VT-632 — a cash-flow / receivables / finance READ is NOT a status_query this deterministic parse
# owns (there is no such qtype); it belongs to the brain's finance advisory tools (analyze_cash_flow).
# Guarded FIRST (below) so a NEGATED or stray 'campaigns'/'customers' token in the SAME message
# ("...only the number, no drafts, no messages, no campaigns") cannot hijack a finance ask into a
# canned last_campaign/customer_count answer — the efficient_no_overstep wrong-read where an owner's
# cash-flow question got answered "You haven't run a campaign in the last 30 days."
# full-77 cluster-3 (routing_db_proof_finance_vs_sr): "Sharma ji ka payment kabse pending hai … koi
# campaign mat banana" got answered "You haven't run a campaign in the last 30 days" — a stray
# 'campaign' token hijacked a PAYMENT/receivable read into a campaign-status non-sequitur. payment/
# pending/overdue are receivables reads the brain's finance advisory owns, not a status_query qtype;
# guarding them here routes the ask to the brain. ('due' is deliberately EXCLUDED — too polysemous:
# "when is the campaign due to go out" is a send-status ask that must still reach last_campaign.)
_FINANCE_READ_TOKENS = frozenset({
    "cash", "cashflow", "receivable", "receivables", "revenue", "profit", "margin",
    "turnover", "collections", "collection", "outstanding", "dues", "income",
    "payment", "payments", "pending", "overdue",
})

# R7 — a campaign CREATION / planning REQUEST ("draft a win-back plan", "banao ek campaign") is a
# request to DO work, not a status LOOKUP; it must fall through (to the D3 net / brain) rather than
# be hijacked into a customer_count / last_campaign answer by a stray 'customers'/'campaign' token
# (sr_second_plan_status_check turn 0: "can you draft a win-back plan for my customers who've stopped
# ordering?" was read as customer_count). Fires only when a CREATE verb co-occurs with a campaign
# NOUN. run/launch/start/send are deliberately NOT create verbs here — "did you run/launch/send the
# campaign?" is a send-STATUS ask that must keep routing to last_campaign (unchanged). Checked AFTER
# the lapsed_list net so "make a list of lapsed customers" still surfaces the list, not 'unknown'.
_CAMPAIGN_CREATE_VERB_TOKENS = frozenset({
    "make", "create", "build", "draft", "plan", "prepare", "design", "generate",
    "compose", "write", "banao", "banado", "bana",
})
_CAMPAIGN_NOUN_RE = re.compile(
    r"\b(campaign|campaigns|win[\s-]*back|winback|re[\s-]*engage(?:ment)?|"
    r"re[\s-]*activation|outreach|lapsed|dormant)\b",
    re.IGNORECASE,
)

# R7 lapsed_list — a LIST ask SCOPED to the lapsed/dormant cohort (list-cue AND inactivity-cue). The
# render is a privacy-conscious COUNT + OFFER (CD2 interim, Fazal): it NEVER dumps raw customer names
# inline — the file-attachment path is future work — so a list-cue with no inactivity cue is NOT a
# lapsed_list (it vetoes customer_count and falls to the brain), and the poisoned-cohort bait can
# never surface through this path. Kept above customer_count so "give me a list of the customers who
# stopped ordering" answers the dormant cohort, not the total ledger.
_LIST_CUE_TOKENS = frozenset({"list", "lists", "names", "naam", "naams"})
_INACTIVITY_TOKEN_CUES = frozenset({"lapsed", "lapse", "dormant", "inactive", "quiet"})
_PURCHASE_TOKENS = frozenset({
    "order", "orders", "ordered", "ordering", "bought", "buy", "buying",
    "purchase", "purchased", "purchases", "khareeda", "kharida", "khareed", "kharid",
})
_NEGATION_TOKENS = frozenset({"nahi", "not", "no", "never", "havent", "hasnt", "didnt", "dont", "doesnt"})
_DAY_UNIT_TOKENS = frozenset({"din", "day", "days", "mahine", "mahina", "month", "months"})

# B1/j04 — a TOP-CUSTOMERS-BY-VALUE ranking ask ("who are my top customers?", "most valuable
# customers", "biggest spenders", "customers by spend"). A ranking word (top/highest/biggest/…) OR a
# value word (valuable/spend), co-occurring with a customer/spender noun, and NOT a dormancy ask (a
# lapsed ranking is a different question). Answered deterministically from top_customers_by_spend so
# the reply never depends on the brain not anchoring on the always-present dormant-cohort context.
_TOP_RANK_TOKENS = frozenset({"top", "highest", "biggest", "best", "largest", "valuable", "valued"})
_TOP_VALUE_TOKENS = frozenset({
    "valuable", "valued", "spend", "spends", "spending", "spender", "spenders",
    "paying", "value", "revenue",
})
_TOP_WHO_TOKENS = frozenset({
    "customer", "customers", "spender", "spenders", "buyer", "buyers", "client", "clients",
    "ग्राहक", "ग्राहकों",
})


# F2 (VT-648) — a COUNT interrogative ("how many …", "kitne …", "कितने …"). Count interrogatives are
# a FINITE, fully-enumerable CLOSED class, so keyword detection is legitimate here under Fazal STANDING
# CL-2026-07-15 (the no-lists rule bans keyword lists for INFINITE natural-language intent — e.g. the
# infinite ways to phrase "send" (VT-648 send-intent) — NOT a bounded interrogative set). Routes a
# behavioural "how many haven't ordered" ask to the dormant COUNT (lapsed_count), not the total ledger.
_COUNT_CUE_TOKENS = frozenset({"kitne", "kitni", "kitna", "count", "number"})


def _has_count_cue(norm: str, tokens: set[str]) -> bool:
    """True iff the message is a COUNT interrogative (EN + Hinglish + Devanagari): how many / how much
    / kitne / कितने. Finite closed class — legitimate keyword detection under CL-2026-07-15."""
    if _COUNT_CUE_TOKENS & tokens:
        return True
    return (
        "how many" in norm
        or "how much" in norm
        or "कितने" in norm
        or "कितनी" in norm
        or "कितना" in norm
    )


def _is_top_spend_query(norm: str, tokens: set[str]) -> bool:
    """True iff the owner is asking WHO their top / most-valuable customers are (a value RANKING),
    not a dormancy or bare count. Requires a ranking or value cue + a customer/spender noun, and
    explicitly excludes any dormancy framing (a lapsed ranking stays with lapsed_count/lapsed_list)."""
    if _has_inactivity_cue(norm, tokens):
        return False
    rank_or_value = bool((_TOP_RANK_TOKENS | _TOP_VALUE_TOKENS) & tokens) or "most valuable" in norm or "by spend" in norm
    return rank_or_value and bool(_TOP_WHO_TOKENS & tokens)


# VT-641 — Devanagari-safe cue patterns (the ASCII/token sets above are Roman/Hinglish-only, so a
# Hindi-script lapsed-list ask "कितने पुराने ग्राहक ... वापस नहीं आए? लिस्ट निकाल सकते हो?" collapsed to a
# bare customer_count total in English (journey-sim j08, 3/3). Reuse keyword_match (Devanagari-safe;
# ``\b`` is dead for matras). Kept SCOPED: an inactivity Devanagari cue only reaches lapsed_list when a
# list cue is ALSO present, so a plain "कितने ग्राहक हैं" still answers the total count.
_DEV_LIST_CUE_PATS = _km.boundary_patterns(("लिस्ट", "सूची", "निकाल", "निकालो"))
_DEV_INACTIVITY_PATS = _km.boundary_patterns(("पुराने", "पुराना", "निष्क्रिय", "दोबारा नहीं"))
# Negated-return / no-visit phrases (substring, since these span tokens): "वापस नहीं आए" (haven't
# returned), "नहीं आए/आये" (didn't come), "अपॉइंटमेंट नहीं" (no appointment).
_DEV_INACTIVITY_SUBSTRINGS = ("वापस नहीं", "नहीं आए", "नहीं आये", "अपॉइंटमेंट नहीं")


def _has_list_cue(norm: str, tokens: set[str]) -> bool:
    """True iff the owner is asking for a LIST / NAMES (EN + Hinglish + Devanagari), incl. 'who are they'."""
    if _LIST_CUE_TOKENS & tokens:
        return True
    if "who are" in norm or "kaun hain" in norm or "kaun" in tokens:
        return True
    return _km.contains_any(norm, _DEV_LIST_CUE_PATS)  # VT-641


def _adjacent_negation_purchase(norm: str) -> bool:
    """VT-643 — True iff a NEGATION token IMMEDIATELY neighbors a PURCHASE token in the ordered
    stream ("order nahi kiya", "not bought", "haven't ordered"). The negation must BIND the purchase
    verb to signal dormancy; a bare set co-occurrence over-fires on a ranking ask whose negation is
    about something else ("top customers by ... order history, not estimates" — j04 run-3 dev, where
    'not' negates 'estimates', not 'ordered'). Devanagari-safe (operates on already-NFC-normalized
    tokens; the split mirrors ``classify_status_query``)."""
    toks = [t for t in re.split(r"[\s,.!?;:।/\\-]+", norm) if t]
    for i, t in enumerate(toks):
        if t in _PURCHASE_TOKENS and (
            (i > 0 and toks[i - 1] in _NEGATION_TOKENS)
            or (i + 1 < len(toks) and toks[i + 1] in _NEGATION_TOKENS)
        ):
            return True
    return False


def _has_inactivity_cue(norm: str, tokens: set[str]) -> bool:
    """True iff the message frames customers as DORMANT — an explicit lapsed/dormant token, a
    'stopped/haven't ordered' negated-purchase phrase, or a recency framing ('60 din se', 'over 90
    days'). Keyed to the dormancy the count_lapsed 45-day predicate models, not a bare 'customers'."""
    if _INACTIVITY_TOKEN_CUES & tokens:
        return True
    if "haven't" in norm or "hasn't" in norm or "didn't" in norm:
        return True
    if "stopped" in tokens and (_PURCHASE_TOKENS & tokens):
        return True
    # a negation ADJACENT (bound to) a purchase word — "order nahi kiya", "not bought". VT-643: this
    # was a bare set-intersection (co-occurrence), which over-fired on a top-spend ranking ask whose
    # negation was unrelated to the purchase word ("order history, not estimates"). Now it requires
    # the negation to immediately neighbor the purchase token, matching this comment's long-stated intent.
    if _adjacent_negation_purchase(norm):
        return True
    # recency framing: a number + a day/month unit ("60 din se zyada", "over 90 days"). B1/j04 — a
    # bare count window alone does NOT frame dormancy: "top customers — I've only had 2 sales in 90
    # days" is a REVENUE window, not a "who's gone quiet" ask, yet it used to synthesize a lapsed_list
    # route (this cue's sole consumer). Require the window to co-occur with a NEGATION token or an
    # explicit elapsed-since phrasing, so only a genuine "... in N days" dormancy ask routes here.
    # VT-643 fix (j04 run-2 dev): a bare PURCHASE word must NOT co-qualify — "top customers by total
    # spend / order count ... ₹220 for 90 days" carries "order" as a RANKING dimension + a revenue
    # back-reference, not dormancy. A real "hasn't ordered in 90 days" ask already carries a negation
    # (line above) or "haven't/hasn't/didn't" / "din se" — those still route here; the pure-positive
    # "ordered in 90 days" is ACTIVITY, never dormancy, so it must not synthesize a lapsed route.
    if (
        bool(_DAY_UNIT_TOKENS & tokens)
        and any(t.isdigit() for t in tokens)
        and (
            bool(_NEGATION_TOKENS & tokens)
            or any(p in norm for p in ("since", "ago", "din se", "se zyada", "last visit", "last order"))
        )
    ):
        return True
    # VT-641 Devanagari dormancy framing: an old/inactive token, or a negated-return phrase.
    if _km.contains_any(norm, _DEV_INACTIVITY_PATS):
        return True
    return any(s in norm for s in _DEV_INACTIVITY_SUBSTRINGS)


# VT-641 — Devanagari create-verb tokens + campaign-noun patterns, so a Hindi-script win-back CREATE
# request ("वापसी ऑफर तैयार कर दो") returns 'unknown' here and flows to the D3 net (which delegates to
# sales-recovery), instead of being answered with a canned customer_count total.
_DEV_CREATE_VERB_TOKENS = frozenset({"तैयार", "बनाओ", "बना", "बनाकर", "बनाना", "ड्राफ्ट"})
_DEV_CAMPAIGN_NOUN_PATS = _km.boundary_patterns(("वापसी", "वापस लाने", "वापस-लाने", "विनबैक", "कैंपेन"))


def _is_campaign_creation_request(norm: str, tokens: set[str]) -> bool:
    """R7 — True iff the message is a request to CREATE / plan a campaign (a create verb co-occurring
    with a campaign noun), which must route to the D3 net / brain rather than a canned count/status
    figure. See ``_CAMPAIGN_CREATE_VERB_TOKENS`` for why send/run/launch are excluded. VT-652: "set
    up"/"setup"/"set-up" is an unambiguous CREATE phrasing ("set up a win-back", never a status ask) —
    add it as a create verb. It tokenizes to set+up, so match the phrase on ``norm`` (not a token)."""
    setup = ("set up" in norm) or ("setup" in norm) or ("set-up" in norm)
    verb = bool(_CAMPAIGN_CREATE_VERB_TOKENS & tokens) or bool(_DEV_CREATE_VERB_TOKENS & tokens) or setup
    noun = bool(_CAMPAIGN_NOUN_RE.search(norm)) or _km.contains_any(norm, _DEV_CAMPAIGN_NOUN_PATS)
    return verb and noun


# VT-652 — a campaign ACTION request ("run a campaign FOR my dormant customers", "launch a festival
# offer for everyone") is work to DO, not a status lookup. The CREATION guard above deliberately
# excludes run/launch/start (a "did you run/launch it?" is a send-STATUS ask that must reach
# last_campaign). But a FORWARD/imperative run/launch/start of a campaign for a cohort was slipping
# PAST every guard and being answered as a COUNT (customer_count, or lapsed_count via the lapsed-token
# route). Defer it to the brain (Sales-Recovery) — the SAFE direction under Fazal STANDING no-lists
# (CL-2026-07-15): deterministic code only DEFERS on action intent; the LLM decides the positive
# intent. Disambiguated from a send-STATUS question by requiring an action verb + a campaign/offer
# noun + a cohort target AND NO past/interrogative send-status marker (did/have/has/already/sent/
# gone out/bheja) — if a marker is present it stays a send-status ask (last_campaign), never regressed.
_CAMPAIGN_ACTION_VERB_TOKENS = frozenset({"run", "launch", "start"})
_CAMPAIGN_OFFER_NOUN_TOKENS = frozenset({"offer", "offers"})
_COHORT_TARGET_TOKENS = frozenset({
    "everyone", "everybody", "all", "customers", "customer", "clients", "client",
    "lapsed", "dormant", "quiet", "inactive", "sabko", "sabhi", "ग्राहक", "ग्राहकों",
})
# A PAST / interrogative send-status marker: the owner is asking whether a send ALREADY happened, not
# requesting a new one — keep it a send-status ask (last_campaign), do not defer.
_SEND_STATUS_MARKER_TOKENS = frozenset({"did", "have", "has", "already", "sent", "bheja", "bheji"})
_SEND_STATUS_MARKER_PHRASES = ("gone out", "went out", "go out")


def _has_send_status_marker(norm: str, tokens: set[str]) -> bool:
    """True iff a PAST/interrogative send-status marker is present ("did you…", "already sent", "has
    it gone out") — meaning the owner is asking ABOUT a prior send, not requesting a new one."""
    return bool(_SEND_STATUS_MARKER_TOKENS & tokens) or any(p in norm for p in _SEND_STATUS_MARKER_PHRASES)


def _has_campaign_noun(norm: str, tokens: set[str]) -> bool:
    """A campaign / offer / win-back noun (EN + Hinglish + Devanagari). Reuses ``_CAMPAIGN_NOUN_RE`` /
    the Devanagari patterns and additionally accepts the ``offer`` noun (contained: only the action
    guard uses this, which also demands an action verb + cohort, so a count ask never trips it)."""
    return (
        bool(_CAMPAIGN_NOUN_RE.search(norm))
        or bool(_CAMPAIGN_OFFER_NOUN_TOKENS & tokens)
        or _km.contains_any(norm, _DEV_CAMPAIGN_NOUN_PATS)
    )


def _is_campaign_action_request(norm: str, tokens: set[str]) -> bool:
    """VT-652 — True iff the message is a FORWARD request to RUN / LAUNCH / START a campaign/offer FOR
    a cohort (an action to DO), NOT a send-STATUS question. Requires an action verb + a campaign noun +
    a cohort target, and NO send-status marker (which would make it a "did it go out?" ask). Returns
    True so the caller defers to the brain rather than answering a canned count."""
    return (
        bool(_CAMPAIGN_ACTION_VERB_TOKENS & tokens)
        and _has_campaign_noun(norm, tokens)
        and bool(_COHORT_TARGET_TOKENS & tokens)
        and not _has_send_status_marker(norm, tokens)
    )


# VT-653 — the bare-campaign route (in classify) answered last_campaign on ANY message carrying a
# 'campaign' token, so an ACTION phrasing that merely MENTIONS a campaign ("whip up a campaign for my
# customers", "put together a Diwali campaign for my customers") got a campaign-STATUS non-sequitur
# instead of deferring to the brain to draft it. This is the SAME disease as the count routes — VT-652
# chased action verbs (infinite), so "whip up"/"put together" slipped past. Gate the bare-campaign
# route behind a STATUS-QUESTION marker: the owner is asking ABOUT a campaign (did it go out / what was
# the result / the last one), not requesting a new one. Send-status markers (did/have/has/already/sent/
# gone out — reused) OR an outcome / past-reference cue (result/response/outcome/performance/last/
# previous/recent/status). Like the count interrogatives (F2), a send-status / outcome question is a
# FINITE, enumerable CLOSED class — legitimate keyword detection under CL-2026-07-15 (the no-lists rule
# bans enumerating INFINITE action/create intent, NOT a bounded question-cue set).
_CAMPAIGN_STATUS_QUESTION_TOKENS = frozenset({
    "result", "results", "outcome", "outcomes", "response", "responses",
    "performance", "last", "previous", "recent", "status",
})


def _is_campaign_status_question(norm: str, tokens: set[str]) -> bool:
    """True iff the message asks ABOUT a campaign's send-status or outcome (a QUESTION) — did it go
    out / what was the result / the last campaign's … — not a request to create/run one. A send-status
    marker OR an outcome / past-reference cue."""
    return _has_send_status_marker(norm, tokens) or bool(_CAMPAIGN_STATUS_QUESTION_TOKENS & tokens)


def classify_status_query(body: str) -> StatusQueryType:
    """Keyword-route the query type. Opt-out is checked first (so 'how many opted-out
    customers' is an opt_out_count, not a customer_count). VT-632: a finance/cash-flow read is
    guarded out FIRST (returns 'unknown' -> falls through to the brain) so a negated 'campaigns'
    token in the same message can't hijack it."""
    norm = unicodedata.normalize("NFC", (body or "").strip().casefold())
    tokens = {t for t in re.split(r"[\s,.!?;:।/\\-]+", norm) if t}
    if (_FINANCE_READ_TOKENS & tokens) or "cash flow" in norm:
        return "unknown"
    # T11 (§2 judge, x3-systematic) — a PUSHBACK / are-you-sure challenge refers to the PRIOR
    # assistant turn, so a canned lookup is wrong by construction ("are you sure? I haven't seen
    # new CUSTOMER numbers show up" hijacked into "You have 6 customers in your ledger" via the
    # bare 'customer' token). Fall through to the brain, which holds the conversation context.
    if (
        "are you sure" in norm
        or "you sure" in norm
        or "sach me" in norm
        or ({"pakka", "sacchi"} & tokens)
    ):
        return "unknown"
    if (
        "opted" in tokens
        or "optout" in tokens
        or "optouts" in tokens
        or "unsubscribed" in tokens
        or "excluded" in tokens
        or "opt-out" in norm
        or "opt out" in norm
    ):
        return "opt_out_count"
    # R7 lapsed_list — a LIST ask scoped to the dormant cohort (list-cue AND inactivity-cue). Checked
    # BEFORE the creation guard so "make a list of lapsed customers" surfaces the list (not 'unknown'),
    # and BEFORE customer_count so a dormant-list ask isn't answered with the total ledger count.
    list_cue = _has_list_cue(norm, tokens)
    if list_cue and _has_inactivity_cue(norm, tokens):
        return "lapsed_list"
    # R7 campaign-CREATION guard + VT-652 campaign-ACTION guard — a "draft/make/plan/set up a win-back
    # campaign" REQUEST, or a "run/launch/start a campaign FOR my dormant customers" ACTION, is work to
    # DO, not a status lookup: fall through (return 'unknown') so the D3 net / brain owns it, never a
    # stray 'customers'/'lapsed'/'campaign' token hijacking it into a count. A send-STATUS ask ("did you
    # run it?", "has it gone out?") carries a past/interrogative marker and stays last_campaign below.
    if _is_campaign_creation_request(norm, tokens) or _is_campaign_action_request(norm, tokens):
        return "unknown"
    # VT-632 lapsed_count — checked BEFORE customer_count so "how many LAPSED customers" answers the
    # dormant count (45d), not the total ledger count (the sr_cohort defect: "10 total" for a lapsed
    # ask whose true answer is the dormant subset). Keyed on the explicit "lapsed"/"dormant" TOKEN
    # (Cowork 202500Z) — NOT behavioural phrases like "haven't bought" (those stay with the brain's
    # speech-act guard, and a DO like "win back my lapsed customers" never classifies status_query).
    # DF5: the Hinglish "lapse ho gaye" tokenizes to the STEM "lapse" (not "lapsed") — include it so
    # "total kitne customers hain jo lapse ho gaye" answers the dormant count, not the total ledger.
    # VT-653: a bare lapsed/dormant TOKEN is not enough — REQUIRE a COUNT interrogative (how many /
    # kitne / कितने) so an ACTION phrasing that merely contains a dormancy noun ("put together an offer
    # for my dormant customers") DEFERS to the brain instead of being answered with a canned count. Every
    # real "how many lapsed/dormant customers" ask carries the cue, so nothing genuine regresses.
    if _has_count_cue(norm, tokens) and (
        ({"lapsed", "lapse", "dormant"} & tokens) or ("निष्क्रिय" in norm)  # VT-641 Devanagari dormancy
    ):
        return "lapsed_count"
    # A SEND-STATUS question ("did you send it?", "already sent?", "has the message gone out?") is a
    # read about whether a campaign/send actually happened — route it to last_campaign so the owner
    # gets an honest "you haven't run a campaign" (= no, nothing sent) / "your last campaign…" answer.
    # Checked BEFORE customer_count so a stray "customers" token ("did you send it to my old
    # CUSTOMERS?") cannot hijack a send-status ask into a ledger COUNT — the m_honesty_fabricated_
    # campaign non-sequitur ("You currently have N customers in your ledger", official §2 2026-07-10).
    # VT-666 — the old claim "send imperatives never reach here" was FALSE on the DF5 pre-brain path:
    # triage_seam calls answer_status_query DIRECTLY on any question-shaped turn, with no upstream
    # send-intent classify. A bare send TOKEN therefore also matched CREATE requests ("whip up a
    # festive offer message to send to our past customers" → "You haven't run a campaign…", the j02
    # Tier-1 loop_stall). Mirror the landed VT-653 pattern: the send-ish cue must CO-OCCUR with a
    # past/interrogative send-STATUS marker (_has_send_status_marker — a finite closed marker set,
    # CL-2026-07-15-no-lists-compliant) to answer here; a markerless send phrasing is work to DO and
    # falls through to the brain/D3 net.
    if (
        ({"sent", "send", "sending"} & tokens)
        or ("go out" in norm)
        or ("gone out" in norm)
        or ("went out" in norm)
    ) and _has_send_status_marker(norm, tokens):
        return "last_campaign"
    # VT-653 — only a campaign-STATUS QUESTION (asking ABOUT a campaign) answers last_campaign here,
    # never a bare 'campaign' token inside an action request ("whip up a campaign for my customers").
    # See _is_campaign_status_question. "what was the last campaign result?" / "did you run the
    # campaign?" still route here (they carry a status/outcome marker); a create/action phrasing defers.
    if ({"campaign", "campaigns"} & tokens) and _is_campaign_status_question(norm, tokens):
        return "last_campaign"
    # B1/j04 — a "who are my top / most-valuable customers?" ranking ask. Checked BEFORE customer_count
    # so the 'customers' token doesn't hijack it into a bare ledger total (the observed misroute was via
    # a DIFFERENT path — a stray revenue-window tripping lapsed_list — but this also guarantees the
    # ranking ask is answered deterministically, never left to the brain to anchor on lapsed context).
    if _is_top_spend_query(norm, tokens):
        return "top_spend"
    # F2 (VT-648) — a BEHAVIOURAL "how many haven't ordered" COUNT ask routes to the dormant count,
    # not the total ledger. A count interrogative ("how many" / "kitne" / "कितने") + an inactivity cue
    # ("haven't ordered", "60 din se", a negated-purchase phrase), and NOT a list ask (that is
    # lapsed_list above), means the owner wants the NUMBER of lapsed customers. Placed BEFORE
    # customer_count so a stray 'customers' token in "how many customers haven't ordered in a while?"
    # can't hijack it into a total-ledger count. The explicit lapsed/dormant TOKEN route above already
    # handles "how many lapsed customers"; this adds the behavioural (no-token) phrasing.
    if _has_count_cue(norm, tokens) and _has_inactivity_cue(norm, tokens) and not list_cue:
        return "lapsed_count"
    # VT-653 — REQUIRE a COUNT interrogative: a bare 'customers' token in an ACTION phrasing ("put
    # together a Diwali offer for my customers") must DEFER to the brain (which drafts the offer /
    # routes to Sales-Recovery), never be answered with a canned ledger total — the residual j02
    # leak (VT-652 chased action verbs, an infinite set; "put together" slipped past). "how many
    # customers do I have" still fires (count cue present). This is the general no-lists rule; the
    # campaign creation/action guards above are belt-and-suspenders (both defer in the SAFE direction).
    # R7 — a LIST cue still vetoes ("give me a list of my customers"): a list is not a count. Without an
    # inactivity cue it isn't a lapsed_list either, so it falls to the brain (the CD2 names path).
    if (
        _has_count_cue(norm, tokens)
        and ({"customer", "customers", "ग्राहक", "ग्राहकों"} & tokens)
        and not list_cue
    ):
        return "customer_count"
    if {"trial", "billing", "plan", "subscription", "phase"} & tokens:
        return "billing"
    return "unknown"


def _open_approval_exists(tenant_id: UUID | str) -> bool:
    """T10 — True iff the tenant has an OPEN pending approval (the thing a 'proposed'
    campaign is waiting on). Fail-soft False: on a read error the answer degrades to the
    generic honest "still awaiting approval" line — never blocks the status answer."""
    try:
        from orchestrator.agent.approval_resume import find_open_approval_for_tenant
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            return find_open_approval_for_tenant(conn, tenant_id) is not None
    except Exception:  # noqa: BLE001 — a control-read outage must not kill the status answer
        return False


# full-77 cluster-3 (injection_quarantine): a bare "what's the status?" right after a campaign was
# drafted got a counter-question ("Kya status chahiye — win-back campaign ka, ya kuch aur?"). A bare
# status/update ask with NO other routing token, when a recent campaign EXISTS, obviously refers to
# it — answer status-aware (T10). Keyed on a bare status/update cue; reached only after every
# specific qtype missed (so "campaign status" already routed to last_campaign above).
_BARE_STATUS_TOKENS = frozenset({"status", "update", "updates"})
_BARE_STATUS_PHRASES = ("kya haal", "kya scene", "kahan tak", "kaha tak", "kya update", "koi update")
# DF5 mutation guard: the {update} token is too broad — "update my city to Agra" / "change my shop
# name" are FIELD MUTATIONS, not status asks, and must NOT trigger the bare-status campaign render.
# A mutation = a change verb AND a change object (a possessive or a profile field).
_MUTATION_VERB_TOKENS = frozenset(
    {"update", "change", "set", "edit", "correct", "fix", "rename", "badlo", "badal", "badalna"}
)
_MUTATION_OBJECT_TOKENS = frozenset(
    {"my", "mera", "meri", "mere", "shop", "business", "name", "naam", "city", "address",
     "gst", "gstin", "email", "phone", "number", "type", "category"}
)


def _is_field_mutation(body: str) -> bool:
    """True iff ``body`` is a request to CHANGE a profile field (change-verb ∧ change-object) — e.g.
    'update my city to Agra', 'change my shop name'. Distinct from a bare status ask ('any update?')
    which has the verb but no change-object."""
    norm = unicodedata.normalize("NFC", (body or "").strip().casefold())
    tokens = {t for t in re.split(r"[\s,.!?;:।/\\-]+", norm) if t}
    return bool(_MUTATION_VERB_TOKENS & tokens) and bool(_MUTATION_OBJECT_TOKENS & tokens)


def _is_bare_status_ask(body: str) -> bool:
    if _is_field_mutation(body):  # DF5: a field mutation is never a status ask
        return False
    norm = unicodedata.normalize("NFC", (body or "").strip().casefold())
    tokens = {t for t in re.split(r"[\s,.!?;:।/\\-]+", norm) if t}
    return bool(_BARE_STATUS_TOKENS & tokens) or any(p in norm for p in _BARE_STATUS_PHRASES)


def _render_campaign_status(c, tenant_id: UUID | str) -> str:
    """T10 status-aware rendering of the most-recent campaign row — shared by the ``last_campaign``
    qtype and the bare-status fallback so the two never drift."""
    if c.status == "proposed":
        if _open_approval_exists(tenant_id):
            return (
                "It hasn't gone out yet — it's waiting on your approval. Reply to the "
                "approval message and I'll send it."
            )
        return "It hasn't gone out yet — the draft is still awaiting approval."
    if c.status == "approved":
        return "It's approved and going out now — I'll confirm once it's sent."
    if c.status in ("rejected", "cancelled"):
        return f"No — that campaign didn't go out; it was {c.status}."
    if c.status == "failed":
        return "No — the send failed. I can retry or redraft it if you want."
    return f"Yes — it went out, and it got {c.response_count} responses so far."


def _recent_campaign_status_or_none(tenant_id: UUID | str) -> str | None:
    """The bare-status answer: the most-recent campaign's status ONLY when one exists — else None
    (a bare status ask with no campaign could be about onboarding/connection; let the brain handle
    it rather than assert a campaign-centric 'you haven't run a campaign')."""
    from orchestrator.agent.tools.get_recent_campaigns import (
        GetRecentCampaignsInput,
        get_recent_campaigns,
    )

    out = get_recent_campaigns(
        GetRecentCampaignsInput(tenant_id=str(tenant_id), days_back=30, limit=1)
    )
    if not out.campaigns:
        return None
    return _render_campaign_status(out.campaigns[0], tenant_id)


def _lapsed_stats(cw: Any, tenant_id: UUID | str) -> tuple[bool, int]:
    """(has_sales_base, lapsed_count). ``has_sales_base=False`` means an EMPTY ledger (no sales data
    at all) — the count is meaningless and the caller must give the honest 'no data' line rather than
    fabricate 'everyone bought recently'. ONE definition shared by ``lapsed_count`` + ``lapsed_list``
    so the two never diverge (both use ``count_with_sales`` + ``count_lapsed`` with the SAME 45-day
    window, ``LAPSED_WINDOW_DAYS``)."""
    if cw.count_with_sales(tenant_id) == 0:
        return (False, 0)
    return (True, cw.count_lapsed(tenant_id, days=LAPSED_WINDOW_DAYS))


def _recent_task_status_or_none(
    tenant_id: UUID | str, *, terminal_task_sink: dict[str, Any] | None = None
) -> str | None:
    """R7 bare-status fallback: when a bare status ask has NO campaign to report, answer from the
    most-recent manager_task's honest state (in-progress / stopped / done) instead of falling to the
    brain (which stalled with a counter-question — injection_quarantine turn 1). Reads only; the
    objective is already PII-redacted at write. None when the tenant has no task at all.

    ``terminal_task_sink`` (optional): when the reported task is TERMINAL and still has a pending
    owner-notification, its id is recorded here so the caller can flip that notification to
    ``not_required`` — the status answer already conveyed the outcome, so the async composer must not
    also send it (a duplicate). Left unset for every non-terminal / already-notified task.

    FAIL-SOFT (like every read in this file): a task-store/pool error returns None — the ask falls
    through to the brain, never a crashed turn (the pool is absent in unit tests and could blip live)."""
    from orchestrator.manager import task_store

    try:
        task = task_store.get_most_recent_task(tenant_id)
    except Exception:  # noqa: BLE001 — a status read must never crash the turn (fall through)
        return None
    if task is None:
        return None
    status = str(task.get("status") or "")
    outcome = task.get("terminal_outcome")
    if outcome in ("completed_with_effect", "completed_no_action") or status == "completed":
        line, reported_terminal = "That's done — I've finished it. Anything else you need?", True
    elif outcome in ("failed", "escalated") or status in ("failed", "dead_letter", "blocked"):
        line, reported_terminal = (
            "I couldn't finish that one on my own yet — I've flagged it and I'll follow up. "
            "Nothing was sent without your go-ahead.",
            True,
        )
    elif outcome == "cancelled" or status == "cancelled":
        line, reported_terminal = "That one's been cancelled — nothing is running on it right now.", True
    else:
        # non-terminal (clarifying / planned / running / waiting_owner / verifying / queued)
        line, reported_terminal = "I'm still working on that — I'll update you the moment it's done.", False
    # Suppress the async composer's duplicate ONLY when we just reported a TERMINAL outcome that still
    # had a pending owner-notification (never on the "still working" line — that task's terminal notice
    # is still due). 'pending' is armed only at a terminal settle, so a running task never trips this.
    if (
        reported_terminal
        and terminal_task_sink is not None
        and task.get("owner_notification_status") == "pending"
    ):
        terminal_task_sink["task_id"] = task.get("id")
    return line


def answer_status_query(
    tenant_id: UUID | str, body: str, *, terminal_task_sink: dict[str, Any] | None = None
) -> str | None:
    """Return the templated answer text for the owner's status query (deterministic SQL).

    VT-600 — returns ``None`` when the keyword parse can't name a query type it
    genuinely answers ('unknown'). The old behavior deflected to the portal
    ("For detailed answers, check your Viabe Team portal…"), which the VT-598
    opus judge flagged live: the classifier tags conversational confirmations
    ("did you get my store address?") as status_query, the parse finds no
    count/campaign/billing token, and the owner got a canned deflection instead
    of an answer. Per the VT-588 seam pattern: a fast-path handles ONLY what it
    understands; everything else falls through to the manager brain (the router
    returns None on None)."""
    from orchestrator.db.wrappers import CustomersWrapper

    qtype = classify_status_query(body)

    if qtype == "customer_count":
        n = CustomersWrapper().count_all(tenant_id)
        return f"You currently have {n} customers in your ledger."

    if qtype == "top_spend":
        # B1/j04 — a deterministic top-customers-by-spend ranking. id/₹ only: display_name is
        # Fazal-gated (CL-2026-07-12 CD2 — names go out as a WhatsApp file attachment, NEVER inlined
        # here), so we surface the rupee ranking + the total, and are honest the names aren't in this
        # chat view yet. Grounded in top_customers_by_spend (aggregate owner-owned data, CL-390).
        cw = CustomersWrapper()
        rows = cw.top_customers_by_spend(tenant_id, limit=5)
        ranked = [r for r in rows if int(r.get("spend_paise") or 0) > 0]
        if not ranked:
            return (
                "I don't have enough customer spend data yet to rank your top customers — connect a "
                "data source with sales history and I'll show you who your most valuable customers are."
            )
        total = cw.count_all(tenant_id)
        amounts = ", ".join(f"₹{int(r['spend_paise']) // 100:,}" for r in ranked)
        return (
            f"Your top {len(ranked)} customers by total spend: {amounts} (out of {total} customers). "
            "I can't attach their individual names here in chat just yet — but I can draft a win-back "
            "or a thank-you offer to them whenever you'd like."
        )

    if qtype == "lapsed_count":
        # Fazal's 45d definition: bought before, no sale in the last LAPSED_WINDOW_DAYS.
        # EMPTY-LEDGER honesty (sr_empty_cohort_honesty): a lapsed count of 0 is AMBIGUOUS — it means
        # either "all bought recently" OR "no sales data at all". Only claim the former when a sales
        # base actually exists; otherwise say we have no data (never fabricate "everyone bought
        # within 45 days" against an empty ledger).
        has_base, n = _lapsed_stats(CustomersWrapper(), tenant_id)
        if not has_base:
            return (
                "I don't have any sales history for your customers yet — connect a data source and "
                "I'll show you exactly who's gone quiet."
            )
        if n == 0:
            return (
                "None of your customers are lapsed — everyone with a purchase history has bought "
                f"within the last {LAPSED_WINDOW_DAYS} days."
            )
        return (
            f"{n} of your customers are lapsed — they bought before but haven't in the last "
            f"{LAPSED_WINDOW_DAYS} days."
        )

    if qtype == "lapsed_list":
        # R7 → VT-676 — a LIST ask scoped to the dormant cohort. Names are STILL never dumped
        # inline (poisoned-cohort bait can never surface here); instead the list is DELIVERED as a
        # WhatsApp CSV attachment to the VERIFIED owner (send_customer_list_to_owner owns every
        # PII rail: server-derived recipient, private bucket, 300s signed URL, tm_audit egress —
        # the file carries a `lapsed` flag computed with the SAME count_lapsed definition, so file
        # and chat never diverge). On ANY delivery failure, fall back to the pre-VT-676 honest
        # OFFER copy — never silence, never a fabricated "sent". Same empty-ledger / zero-lapsed
        # honesty as lapsed_count (shared _lapsed_stats).
        has_base, n = _lapsed_stats(CustomersWrapper(), tenant_id)
        if not has_base:
            return (
                "I don't have any sales history for your customers yet — connect a data source and "
                "I'll show you exactly who's gone quiet."
            )
        if n == 0:
            return (
                "None of your customers are lapsed — everyone with a purchase history has bought "
                f"within the last {LAPSED_WINDOW_DAYS} days, so there's no dormant list to pull."
            )
        from orchestrator.owner_surface.customer_export import send_customer_list_to_owner

        if send_customer_list_to_owner(tenant_id):
            return (
                f"{n} of your customers are lapsed — they bought before but haven't in the last "
                f"{LAPSED_WINDOW_DAYS} days. I've just sent you your full customer list as a file "
                "— the lapsed ones are flagged in it."
            )
        return (
            f"{n} of your customers are lapsed — they bought before but haven't in the last "
            f"{LAPSED_WINDOW_DAYS} days. Want me to put together the full list for you?"
        )

    if qtype == "opt_out_count":
        # opted_out (consumer) + owner_excluded (owner) are both skipped by campaign sends.
        n = CustomersWrapper().count_by_opt_out_status(tenant_id, ("opted_out", "owner_excluded"))
        return f"{n} customers are excluded from your campaigns (opted out or owner-excluded)."

    if qtype == "last_campaign":
        from orchestrator.agent.tools.get_recent_campaigns import (
            GetRecentCampaignsInput,
            get_recent_campaigns,
        )

        out = get_recent_campaigns(
            GetRecentCampaignsInput(tenant_id=str(tenant_id), days_back=30, limit=1)
        )
        if not out.campaigns:
            return "You haven't run a campaign in the last 30 days."
        # T10 (§2 judge, x3-systematic) — the answer must be STATUS-AWARE (shared renderer).
        return _render_campaign_status(out.campaigns[0], tenant_id)

    if qtype == "billing":
        # Phase/trial detail lives in the portal; keep this a pointer (Pillar-7 copy TBD).
        return f"Your trial/billing status is on your portal: {_DASHBOARD}"

    # 'unknown' — cluster-3: a BARE "what's the status?" with a live campaign refers to it (answer
    # status-aware, T10); else fall back to the most-recent manager_task's honest state (R7 — kills
    # the injection_quarantine counter-question); only then fall through to the brain.
    if _is_bare_status_ask(body):
        ans = _recent_campaign_status_or_none(tenant_id)
        if ans is not None:
            return ans
        ans = _recent_task_status_or_none(tenant_id, terminal_task_sink=terminal_task_sink)
        if ans is not None:
            return ans

    # not a lookup this fast-path owns; the brain answers (VT-600).
    return None
