"""D3 (subsumes cluster-5b) — the deterministic CAMPAIGN first-contact deciders.

Pure detector + copy coverage (dep-less). The DB-backed net behavior (empty-cohort reply vs
sales_recovery dispatch inside ``triage_seam``) is validated on deployed dev — this file pins the
pure classification/copy contract the net depends on.
"""

from __future__ import annotations

import pytest

# The detector's opt-out/DSR guard imports the canonical matcher from ``pre_filter_gate`` (which
# imports ``dbos`` at module load), and the cohort read imports ``orchestrator.db.wrappers``
# (psycopg). Both are present in the DB-backed orchestrator CI job + locally, absent in the
# dep-less smoke — importorskip so the smoke SKIPS (never a masked import-trap failure).
pytest.importorskip("dbos")
pytest.importorskip("psycopg")

from orchestrator.onboarding import campaign_first_contact as cfc  # noqa: E402


# ----------------------------- imperative detector: POSITIVE -----------------------------
def test_clear_winback_imperatives_fire() -> None:
    for msg in [
        "run a win-back campaign for my lapsed customers",
        "start a re-engagement campaign",
        "launch a winback to dormant customers",
        "send a win-back to my lapsed customers",
        "set up a re-activation campaign",
        # PLANNING verbs (the delegation-lane stall root — "make me a plan"/"plan a campaign")
        "make me a plan to win back my lapsed customers",
        "plan a win-back campaign",
        "prepare a win-back for my lapsed customers",
        "put together a campaign for lapsed customers",
        # Hinglish
        "lapsed customers ko win-back campaign chalao",
        "purane customers ko campaign bhejo",
        "campaign shuru karo",
    ]:
        assert cfc.is_campaign_plan_imperative(msg) is True, msg


def test_planning_verbs_need_a_campaign_noun() -> None:
    # The planning verbs stay tight: no campaign NOUN -> no fire (VERB∧NOUN).
    for msg in ["make it faster", "plan my day", "make a payment of 500", "prepare the invoice"]:
        assert cfc.is_campaign_plan_imperative(msg) is False, msg


def test_external_ad_campaign_does_not_hijack_the_winback_net() -> None:
    # Regression guard: a paid external-ad ask carries an ad-platform token + the generic "campaign"
    # noun, but it is NOT a win-back -> must fall through to the brain, never the no-data reply.
    for msg in [
        "run a Facebook ad campaign for me",
        "run an instagram ad campaign",
        "launch a google ads campaign",
    ]:
        assert cfc.is_campaign_plan_imperative(msg) is False, msg
    # A genuine win-back (no ad-platform token, OR a recovery noun present) still fires.
    assert cfc.is_campaign_plan_imperative("run a win-back campaign for my lapsed customers") is True
    assert cfc.is_campaign_plan_imperative("plan a re-engagement campaign for dormant customers") is True


# ----------------------------- imperative detector: NEGATIVE -----------------------------
def test_bare_noun_or_verb_alone_does_not_fire() -> None:
    # NOUN without a campaign VERB, or VERB without a campaign NOUN.
    assert cfc.is_campaign_plan_imperative("i have some lapsed customers") is False
    assert cfc.is_campaign_plan_imperative("run a report on sales") is False


def test_questions_never_fire() -> None:
    """An imperative is a COMMAND, not a QUESTION — a status/how-to ask routes to the brain."""
    for msg in [
        "how many lapsed customers do I have?",
        "how many lapsed customers do I have",   # no '?' but leading interrogative
        "did you send the campaign?",
        "should I run a win-back campaign?",
        "kitne lapsed customers hain",
        "what happened to my re-engagement campaign?",
    ]:
        assert cfc.is_campaign_plan_imperative(msg) is False, msg


def test_polite_request_form_fires_but_capability_question_does_not() -> None:
    """R7 — a POLITE-REQUEST imperative ("can you draft ... for my customers?") IS a dispatch, even
    with a trailing '?' / interrogative lead; a bare CAPABILITY question ("can you run campaigns?" —
    no first-person beneficiary) still falls to the brain."""
    for msg in [
        "can you draft a win-back plan for my customers who've stopped ordering?",
        "could you please prepare a re-engagement for my lapsed customers",
        "would you set up a win-back campaign for me?",
    ]:
        assert cfc.is_campaign_plan_imperative(msg) is True, msg
    for msg in [
        "can you run campaigns?",           # no beneficiary -> capability question
        "could you build campaigns?",        # no beneficiary
        "should I run a win-back campaign?",  # 'should' is not a polite-request lead
    ]:
        assert cfc.is_campaign_plan_imperative(msg) is False, msg


def test_optout_dsr_never_read_as_campaign() -> None:
    """DPDP: opt-out / DSR wins first — never interpreted as a request to run a campaign."""
    for msg in ["stop everything", "band karo", "please delete my data", "STOP"]:
        assert cfc.is_campaign_plan_imperative(msg) is False, msg


def test_spend_boost_ask_does_not_fire() -> None:
    """The sr_spend_ceiling boost/₹ ask has NO campaign noun — the money-approval path owns it, not
    this net (guards against a regression that would hijack the spend-ceiling scenario)."""
    assert (
        cfc.is_campaign_plan_imperative(
            "mere last Instagram post ko 500 rupaye dekar boost kar do"
        )
        is False
    )


def test_empty_and_blank_do_not_fire() -> None:
    assert cfc.is_campaign_plan_imperative("") is False
    assert cfc.is_campaign_plan_imperative("   ") is False
    assert cfc.is_campaign_plan_imperative(None) is False  # type: ignore[arg-type]


# ----------------------------- empty-cohort read: FAIL-OPEN ------------------------------
def test_cohort_read_is_fail_open_false(monkeypatch) -> None:
    """A read error must NEVER fabricate 'you have no data' — fail-open returns False (cohort might
    exist), so the honest empty-cohort message is never sent on a transient blip."""
    import orchestrator.onboarding.campaign_first_contact as mod

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("db down")

    # The wrapper import happens inside the fn; patch the class it resolves so .count_with_sales blows up.
    import orchestrator.db.wrappers as w

    monkeypatch.setattr(w.CustomersWrapper, "count_with_sales", _boom, raising=True)
    assert mod.campaign_cohort_is_empty("00000000-0000-0000-0000-000000000000") is False


# ----------------------------- honest copy contract -------------------------------------
def test_empty_cohort_reply_is_honest_and_actionable() -> None:
    body = cfc.EMPTY_COHORT_REPLY
    low = body.lower()
    # Never a FALSE past/present completion claim (a campaign already ran / messages already sent).
    for bad in ["i've started", "has started", "is running", "i've sent", "i sent", "already sent"]:
        assert bad not in low, bad
    # Names the concrete fix so it is actionable, not a dead end.
    assert "Sheet" in body or "Shopify" in body


def test_vt641_devanagari_winback_imperative_delegates() -> None:
    """VT-641 — a Hindi-script win-back imperative fires D3 like its Roman twin (journey-sim j08 3/3).
    ASCII \\b is dead for Devanagari matras, so these matched neither regex pre-fix."""
    for msg in [
        "इन 8 ग्राहकों के लिए एक अच्छा सा वापसी ऑफर तैयार कर दो, पर अभी भेजना मत, पहले दिखाओ",
        "पुराने ग्राहकों के लिए वापसी ऑफर बनाओ",
        "इन ग्राहकों को वापस लाने वाला कैंपेन तैयार कर दो",
    ]:
        assert cfc.is_campaign_plan_imperative(msg) is True, msg


def test_vt641_devanagari_status_question_does_not_fire() -> None:
    """VT-641 — a Devanagari count/list QUESTION (no campaign VERB∧NOUN imperative) falls through."""
    for msg in [
        "कितने पुराने ग्राहक वापस नहीं आए? एक लिस्ट निकाल सकते हो?",
        "क्या तुमने कैंपेन भेज दिया?",
    ]:
        assert cfc.is_campaign_plan_imperative(msg) is False, msg


# ----------------------------- VT-642: co-present list-send acknowledgment ----------------
def test_vt642_list_send_cue_detected_alongside_winback() -> None:
    """VT-642 — a win-back message that ALSO asks for the LIST / the names co-carries a list-send
    speech-act (journey j08 turn-2). The predicate flags it so the caller rides an honest
    can't-attach-names-yet ack alongside the draft (the ask is never silently dropped)."""
    for msg in [
        # EN / Hinglish
        "send me the list and prepare a win-back offer for them",
        "share the names of the lapsed customers, don't send anything yet",
        "haan wo list bhej do mujhe",
        "in customers ke naam bhejo aur offer taiyaar karo",
        # Devanagari (journey j08 turn-2 phrasing)
        "हां, वो 6 वाली लिस्ट भेज दो मुझे। उनके लिए एक वापसी ऑफर भी तैयार कर लो",
        "उन ग्राहकों की सूची भेज दो और वापस लाने वाला ऑफर बनाओ",
        "इन ग्राहकों के नाम भेजो",
    ]:
        assert cfc.mentions_customer_list_request(msg) is True, msg


def test_vt642_bare_winback_has_no_list_cue() -> None:
    """A win-back imperative with NO list/names ask must NOT trigger the list-send ack (no noise)."""
    for msg in [
        "run a win-back campaign for my lapsed customers",
        "इन 8 ग्राहकों के लिए वापसी ऑफर तैयार कर दो, अभी भेजना मत",  # offer, no list/names cue
        "start a re-engagement campaign",
        "purane customers ko campaign bhejo",  # 'bhejo' = send campaign, not send-the-list
    ]:
        assert cfc.mentions_customer_list_request(msg) is False, msg


def test_vt642_list_ack_is_honest_and_reaffirms_the_money_gate() -> None:
    body = cfc.LIST_SEND_ACK_PREAMBLE
    low = body.lower()
    # Honest capability bound (can't attach names yet) — never a false "here they are" name dump.
    assert "can't" in low or "cannot" in low
    # Bridges to the draft AND re-affirms the money gate (nothing sends without approval).
    assert "draft" in low or "drafting" in low
    assert "approve" in low or "until you say so" in low
    # Never a FALSE completion claim (nothing already sent).
    for bad in ["i've sent", "i sent", "already sent", "has gone out"]:
        assert bad not in low, bad
