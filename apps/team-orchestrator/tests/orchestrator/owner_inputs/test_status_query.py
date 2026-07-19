"""R7 — the owner status-query classifier + deterministic answer nets (pure / dep-light).

``classify_status_query`` is pure string parsing; the render branches are exercised with the DB
reads monkeypatched (no live Postgres). Importing the module pulls ``psycopg`` (via
``orchestrator.db.wrappers``), so importorskip so the dep-less smoke SKIPS this file cleanly.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.owner_inputs import status_query as sq  # noqa: E402

_TID = str(uuid4())


# ----------------------------- classify: the R7 routing table -----------------------------
def test_classify_campaign_creation_request_falls_through_to_brain() -> None:
    # A CREATE/plan request is work to DO, not a lookup — must NOT be answered with a count.
    for msg in [
        "can you draft a win-back plan for my customers who've stopped ordering?",
        "make me a win-back campaign for my lapsed customers",
        "please prepare a re-engagement campaign for me",
        "draft a plan to reach my lapsed customers",
    ]:
        assert sq.classify_status_query(msg) == "unknown", msg


def test_classify_send_status_still_routes_last_campaign() -> None:
    # 'send'/'run' are NOT create verbs — a send-STATUS ask keeps routing to last_campaign.
    assert sq.classify_status_query("did you send the campaign to my customers?") == "last_campaign"
    assert sq.classify_status_query("did you run the campaign?") == "last_campaign"
    assert sq.classify_status_query("has the message gone out yet?") == "last_campaign"


def test_vt652_campaign_action_request_defers_to_brain() -> None:
    # VT-652 — a FORWARD "set up / run / launch a campaign/offer FOR a cohort" is an ACTION to DO, not
    # a count lookup. It must DEFER to the brain (unknown), never be answered with a canned
    # customer_count / lapsed_count (the ignored_speech_act Tier-1 breaker). The 3 dev-confirmed leaks
    # (were customer_count / lapsed_count / lapsed_count) + an adversarial launch-offer pin.
    for msg in [
        "Can you set up a win-back offer for my customers who've gone quiet?",  # was customer_count
        "set up a win-back for my lapsed customers",                            # was lapsed_count
        "run a campaign for my dormant customers",                              # was lapsed_count
        "launch a festival offer for everyone",                                # adversarial
    ]:
        assert sq.classify_status_query(msg) == "unknown", msg


def test_vt652_action_guard_does_not_regress_send_status() -> None:
    # The action guard must NOT swallow a send-STATUS question: a past/interrogative marker
    # (did/have/has/gone out) keeps it a "did it go out?" ask → last_campaign, never deferred.
    assert sq.classify_status_query("did you run the campaign?") == "last_campaign"
    assert (
        sq.classify_status_query("have you run the winback campaign for everyone yet?")
        == "last_campaign"
    )
    assert sq.classify_status_query("has the campaign gone out?") == "last_campaign"
    assert (
        sq.classify_status_query("did you already send that offer to my customers?")
        == "last_campaign"
    )


def test_vt652_action_guard_does_not_regress_counts() -> None:
    # A legit count ask carries no action verb, so the guard leaves it alone.
    assert sq.classify_status_query("how many customers do I have?") == "customer_count"
    assert sq.classify_status_query("how many dormant customers") == "lapsed_count"
    assert sq.classify_status_query("how many lapsed customers?") == "lapsed_count"


def test_vt653_count_cue_required_action_phrasings_defer() -> None:
    # VT-653 — the residual j02 leak: VT-652 chased action VERBS (infinite set), so "put together" /
    # "whip up" slipped past and a bare 'customers'/'dormant'/'campaign' NOUN was answered with a
    # canned count/last_campaign. A count/status net now fires ONLY on an actual QUESTION, so every
    # action phrasing DEFERS to the brain (unknown) — which drafts the offer / routes to Sales-Recovery.
    for msg in [
        "put together a Diwali win-back offer for my customers",   # dev-confirmed → was customer_count
        "can you put together a festival offer for my customers",  # dev-confirmed → was customer_count
        "put together an offer for my dormant customers",          # bare 'dormant' noun, no count cue
        "whip up a campaign for my customers",                     # bare 'campaign' noun → was last_campaign
        "put together a Diwali campaign for my customers",         # action + campaign noun, no status marker
    ]:
        got = sq.classify_status_query(msg)
        assert got == "unknown", f"{msg!r} -> {got} (must DEFER, not a count/last_campaign route)"


def test_vt653_count_cue_required_does_not_regress_questions() -> None:
    # Adversarial: a genuine count/status QUESTION carries the interrogative cue, so it still fires.
    assert sq.classify_status_query("how many customers") == "customer_count"
    assert sq.classify_status_query("how many dormant customers") == "lapsed_count"
    # A campaign STATUS/outcome question still routes to last_campaign (send-status OR outcome marker).
    assert sq.classify_status_query("what was the last campaign result?") == "last_campaign"


def test_classify_lapsed_count_unchanged() -> None:
    assert sq.classify_status_query("how many lapsed customers?") == "lapsed_count"
    assert sq.classify_status_query("and how many lapsed customers do I have in total?") == "lapsed_count"
    assert sq.classify_status_query("total kitne customers hain mere paas jo lapse ho gaye?") == "lapsed_count"


def test_classify_lapsed_list_needs_list_cue_and_inactivity_cue() -> None:
    # BOTH cohort-scenario phrasings -> lapsed_list.
    assert (
        sq.classify_status_query(
            "who exactly are the customers that have gone quiet on me? give me a few names to start with"
        )
        == "lapsed_list"
    )
    assert (
        sq.classify_status_query(
            "kaafi customers hain jinhone 60 din se zyada order nahi kiya — unki list bhej do mujhe"
        )
        == "lapsed_list"
    )
    assert sq.classify_status_query("make a list of lapsed customers") == "lapsed_list"


def test_classify_bare_list_ask_routes_to_customer_list() -> None:
    # VT-676 F1 (SUPERSEDES the pre-VT-676 "names path falls to the brain" pin): a plain
    # customer-list ask now DELIVERS the CSV to the verified owner — names never inline, so the
    # poisoned-cohort protection holds by construction (the file path, not a brain dump).
    assert sq.classify_status_query("give me a list of my customers") == "customer_list"
    assert sq.classify_status_query("list all my customer names") == "customer_list"
    assert sq.classify_status_query("send me my customer list") == "customer_list"  # the canary ask
    # A plain count ask (no list cue) still routes to customer_count.
    assert sq.classify_status_query("how many customers do I have?") == "customer_count"
    # Ranking + dormancy scopes are untouched (position guards): they classify before customer_list.
    assert sq.classify_status_query("who are my top customers by spend?") == "top_spend"
    assert sq.classify_status_query("make a list of lapsed customers") == "lapsed_list"


def test_classify_finance_and_billing_unchanged() -> None:
    assert sq.classify_status_query("Sharma ji ka payment kabse pending hai") == "unknown"
    assert sq.classify_status_query("what's my plan?") == "billing"
    assert sq.classify_status_query("how many opted-out customers?") == "opt_out_count"


# ----------------------------- _is_bare_status_ask -----------------------------
def test_is_bare_status_ask() -> None:
    assert sq._is_bare_status_ask("what's the status?") is True
    assert sq._is_bare_status_ask("any update on that?") is True
    assert sq._is_bare_status_ask("kya haal hai?") is True
    # A field mutation carries 'update' but is NOT a status ask.
    assert sq._is_bare_status_ask("update my city to Agra") is False
    # A specific count ask is not a BARE status ask.
    assert sq._is_bare_status_ask("how many customers do I have?") is False


# ----------------------------- lapsed_list render: CD2 count+offer, NO names -----------------------------
class _FakeCW:
    def __init__(self, *, with_sales: int, lapsed: int) -> None:
        self._with_sales = with_sales
        self._lapsed = lapsed

    def count_with_sales(self, _tid: object) -> int:  # noqa: D401
        return self._with_sales

    def count_lapsed(self, _tid: object, *, days: int) -> int:  # noqa: ARG002
        return self._lapsed


def _patch_cw(monkeypatch: pytest.MonkeyPatch, cw: _FakeCW) -> None:
    import orchestrator.db.wrappers as w

    monkeypatch.setattr(w, "CustomersWrapper", lambda: cw)


def _patch_export(monkeypatch: pytest.MonkeyPatch, *, delivered: bool) -> None:
    import orchestrator.owner_surface.customer_export as ce

    monkeypatch.setattr(ce, "send_customer_list_to_owner", lambda tid: delivered)


def test_lapsed_list_render_is_count_plus_offer_never_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """VT-676 fallback arm: export delivery FAILS → the pre-VT-676 honest count+OFFER copy (never a
    fabricated 'sent', never names inline)."""
    _patch_cw(monkeypatch, _FakeCW(with_sales=6, lapsed=5))
    _patch_export(monkeypatch, delivered=False)
    ans = sq.answer_status_query(
        _TID, "kaafi customers hain jinhone 60 din se zyada order nahi kiya — unki list bhej do mujhe"
    )
    assert ans is not None
    assert "5" in ans and str(sq.LAPSED_WINDOW_DAYS) in ans
    # Fallback: the render OFFERS the list, it never dumps names inline and never claims a send.
    assert "list" in ans.lower()
    assert "sent" not in ans.lower()


def test_lapsed_list_delivers_the_file_when_export_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VT-676 primary arm: export delivery SUCCEEDS → the reply states the grounded count AND that
    the file was sent (a DB-backed claim — send_customer_list_to_owner returned True only after a
    real transport sid + audit row). Still never names inline."""
    _patch_cw(monkeypatch, _FakeCW(with_sales=6, lapsed=5))
    _patch_export(monkeypatch, delivered=True)
    ans = sq.answer_status_query(_TID, "make a list of lapsed customers")
    assert ans is not None
    assert "5" in ans and str(sq.LAPSED_WINDOW_DAYS) in ans
    # Fix-4c: the ack COMPLEMENTS the media message ("the file just above") instead of
    # re-claiming a second send — two success-claims doubled the damage when the live
    # canary's media attach failed.
    assert "file" in ans.lower()
    assert "flagged" in ans.lower()  # points the owner at the lapsed flag in the CSV


def test_lapsed_list_empty_ledger_is_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw(monkeypatch, _FakeCW(with_sales=0, lapsed=0))
    ans = sq.answer_status_query(_TID, "give me a list of lapsed customers")
    assert ans is not None and "sales history" in ans.lower()


def test_lapsed_count_and_list_share_the_same_number(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw(monkeypatch, _FakeCW(with_sales=9, lapsed=3))
    _patch_export(monkeypatch, delivered=False)  # VT-676: pin the count invariant, not delivery
    count_ans = sq.answer_status_query(_TID, "how many lapsed customers?")
    list_ans = sq.answer_status_query(_TID, "make a list of lapsed customers")
    assert count_ans is not None and list_ans is not None
    assert "3" in count_ans and "3" in list_ans  # ONE definition (_lapsed_stats)


# ----------------------------- bare-status: campaign path wins, then task fallback -----------------------------
def test_proposed_campaign_status_line_is_approval_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression pin (injection_quarantine runs 1-2): a bare status ask with a PROPOSED campaign +
    an open approval must answer the approval-pending line — not the task fallback."""
    monkeypatch.setattr(sq, "_open_approval_exists", lambda _tid: True)

    class _C:
        status = "proposed"
        response_count = 0

    line = sq._render_campaign_status(_C(), _TID)
    assert "approval" in line.lower()


def test_campaign_path_wins_over_task_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sq, "_recent_campaign_status_or_none", lambda _tid: "CAMPAIGN LINE")

    def _boom(*_a: object, **_k: object):  # get_most_recent_task must NOT be reached
        raise AssertionError("task fallback must not run when a campaign exists")

    monkeypatch.setattr("orchestrator.manager.task_store.get_most_recent_task", _boom)
    assert sq.answer_status_query(_TID, "what's the status?") == "CAMPAIGN LINE"


def _patch_no_campaign_and_task(monkeypatch: pytest.MonkeyPatch, task: dict | None) -> None:
    monkeypatch.setattr(sq, "_recent_campaign_status_or_none", lambda _tid: None)
    monkeypatch.setattr("orchestrator.manager.task_store.get_most_recent_task", lambda _tid: task)


def test_task_fallback_active_task_is_in_progress_no_suppress(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_no_campaign_and_task(
        monkeypatch,
        {"id": uuid4(), "status": "running", "terminal_outcome": None,
         "owner_notification_status": "not_required"},
    )
    sink: dict = {}
    ans = sq.answer_status_query(_TID, "what's the status?", terminal_task_sink=sink)
    assert ans is not None and "still working" in ans.lower()
    assert sink == {}  # a running task never trips the notification-suppress sink


def test_task_fallback_escalated_task_stopped_line_and_suppress(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = uuid4()
    _patch_no_campaign_and_task(
        monkeypatch,
        {"id": task_id, "status": "blocked", "terminal_outcome": "escalated",
         "owner_notification_status": "pending"},
    )
    sink: dict = {}
    ans = sq.answer_status_query(_TID, "any update?", terminal_task_sink=sink)
    assert ans is not None and "couldn't finish" in ans.lower()
    # Reported a terminal outcome with a pending notification -> flag it for suppression.
    assert sink.get("task_id") == task_id


def test_task_fallback_completed_task_done_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_no_campaign_and_task(
        monkeypatch,
        {"id": uuid4(), "status": "completed", "terminal_outcome": "completed_with_effect",
         "owner_notification_status": "delivered"},
    )
    sink: dict = {}
    ans = sq.answer_status_query(_TID, "what's the status?", terminal_task_sink=sink)
    assert ans is not None and "done" in ans.lower()
    assert sink == {}  # already delivered -> nothing to suppress


def test_task_fallback_none_when_no_task(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_no_campaign_and_task(monkeypatch, None)
    # No campaign AND no task -> the fast-path owns nothing; the brain answers (None).
    assert sq.answer_status_query(_TID, "what's the status?") is None


def test_vt641_devanagari_lapsed_list() -> None:
    """VT-641 — a Devanagari lapsed-LIST ask classifies as lapsed_list, not a bare customer_count."""
    assert sq.classify_status_query(
        "कितने पुराने ग्राहक ऐसे हैं जिन्होंने अपॉइंटमेंट नहीं ली या वापस नहीं आए? एक लिस्ट निकाल सकते हो?"
    ) == "lapsed_list"


def test_vt641_devanagari_winback_create_falls_through_to_d3() -> None:
    """VT-641 — a Devanagari win-back CREATE request returns 'unknown' so the D3 net delegates to SR."""
    assert sq.classify_status_query(
        "इन 8 ग्राहकों के लिए वापसी ऑफर तैयार कर दो, पर अभी भेजना मत, पहले दिखाओ"
    ) == "unknown"


def test_vt641_devanagari_plain_count_still_customer_count() -> None:
    """VT-641 regression — a plain Devanagari count ask (no list/inactivity cue) stays customer_count."""
    assert sq.classify_status_query("कितने ग्राहक हैं?") == "customer_count"


def test_vt641_devanagari_inactive_token_lapsed_count() -> None:
    """VT-641 — a Devanagari explicit-dormancy token (निष्क्रिय) routes to lapsed_count."""
    assert sq.classify_status_query("कितने ग्राहक निष्क्रिय हैं?") == "lapsed_count"


# ----------------------------- B1/j04: top-customers-by-spend routing + render -----------------------------
def test_classify_top_spend_ranking_asks() -> None:
    for msg in [
        "who are my top customers?",
        "Who are my top customers by total spend?",
        "show me my most valuable customers",
        "biggest spenders",
        "which are my highest value customers",
        "kaun mere sabse zyada value wale customers hain",
    ]:
        assert sq.classify_status_query(msg) == "top_spend", msg


def test_classify_top_spend_not_hijacked_by_a_stray_revenue_window() -> None:
    # B1 root cause: a revenue time-window in the same message must NOT synthesize a dormancy route.
    assert sq.classify_status_query(
        "I've only had 2 sales in 90 days. Who are my top customers?"
    ) == "top_spend"


def test_classify_top_spend_with_order_metric_and_revenue_backref() -> None:
    # VT-643 j04 run-2 (deployed dev, slipped past the "sales"-only revenue test): a top-value ask
    # whose ranking metric is "order count" and which back-references the prior revenue turn ("₹220
    # for 90 days") must stay top_spend. Regression: "order" (a RANKING dimension) + "90 days" (a
    # revenue back-reference, no negation/elapsed-since) previously tripped the recency inactivity
    # cue -> lapsed_list. A bare purchase word no longer co-qualifies the day-window as dormancy.
    assert sq.classify_status_query(
        "wait ₹220 total for 90 days? that seems way off but ok, separate issue. pull up my top "
        "customers by total spend / order count - who are my most valuable ones right now"
    ) == "top_spend"


def test_classify_top_spend_yields_to_dormancy() -> None:
    # A dormancy-framed ranking is NOT top_spend (a lapsed question owns it, not a value ranking).
    assert sq.classify_status_query("top customers who haven't ordered in a while") != "top_spend"


def test_classify_top_spend_with_unbound_negation_and_purchase_source() -> None:
    # VT-643 j04 run-3 (deployed dev): "top customers by total spend - actual numbers from order
    # history, not estimates" — 'not' negates 'estimates', NOT 'ordered'; 'order history' is a ranking
    # SOURCE. The old bare (purchase & negation) set-cue over-fired -> customer_count. Adjacency binds
    # them now, so this stays a deterministic top_spend (never left to the brain to answer by luck).
    assert sq.classify_status_query(
        "ok fine, tell me who my top customers are by total spend - actual numbers from order "
        "history, not estimates"
    ) == "top_spend"


def test_adjacent_negation_purchase_binds_the_verb() -> None:
    # A negation must NEIGHBOR the purchase word to signal dormancy (money-adjacent: feeds lapsed_count).
    assert sq._adjacent_negation_purchase("order nahi kiya") is True       # Hinglish "didn't order"
    assert sq._adjacent_negation_purchase("customers who have not bought") is True
    assert sq._adjacent_negation_purchase("order history, not estimates") is False  # unrelated 'not'


def test_inactivity_cue_bare_day_window_needs_a_dormancy_cocue() -> None:
    # The bare digit+day-unit recency rule (sole consumer: lapsed_list) no longer fires alone.
    assert sq._has_inactivity_cue("2 sales in 90 days", {"2", "sales", "in", "90", "days"}) is False
    # ...but a genuine "in N days" dormancy ask (purchase + negation, or an elapsed-since phrasing) does.
    assert sq._has_inactivity_cue(
        "no order in 90 days", {"no", "order", "in", "90", "days"}
    ) is True
    assert sq._has_inactivity_cue("90 days since last visit", {"90", "days", "last", "visit"}) is True


class _FakeTopCW:
    def __init__(self, *, total: int, rows: list) -> None:
        self._total = total
        self._rows = rows

    def count_all(self, _tid: object) -> int:
        return self._total

    def top_customers_by_spend(self, _tid: object, *, limit: int, conn: object = None) -> list:  # noqa: ARG002
        return self._rows[:limit]


def test_top_spend_render_is_rupee_ranking_no_names(monkeypatch: pytest.MonkeyPatch) -> None:
    import orchestrator.db.wrappers as w
    rows = [{"spend_paise": 185000, "display_name": "Asha", "phone_e164": "+15550001"},
            {"spend_paise": 120000, "display_name": "Ravi", "phone_e164": "+15550002"}]
    monkeypatch.setattr(w, "CustomersWrapper", lambda: _FakeTopCW(total=10, rows=rows))
    ans = sq.answer_status_query(_TID, "who are my top customers by spend?")
    assert "₹1,850" in ans and "₹1,200" in ans      # rupee ranking surfaced
    assert "10 customers" in ans                     # total grounded
    assert "Asha" not in ans and "Ravi" not in ans   # names Fazal-gated, never inlined
    assert "+1555" not in ans                         # phone never leaks


def test_top_spend_render_empty_is_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    import orchestrator.db.wrappers as w
    monkeypatch.setattr(w, "CustomersWrapper", lambda: _FakeTopCW(total=0, rows=[]))
    ans = sq.answer_status_query(_TID, "who are my top customers?")
    assert "don't have enough" in ans.lower() or "connect" in ans.lower()


def test_vt666_send_create_request_defers_to_brain() -> None:
    # VT-666 (j02 Tier-1 loop_stall) — a campaign-CREATE phrasing that merely contains a send
    # token must NOT be answered as send-STATUS ("You haven't run a campaign in the last 30
    # days"). The send-ish cue now requires a co-occurring past/interrogative send-status MARKER
    # (_has_send_status_marker) — the exact VT-653 pattern applied to the send-token route.
    assert (
        sq.classify_status_query(
            "can you whip up a festive offer message to send to our past customers?"
        )
        != "last_campaign"
    )
    assert (
        sq.classify_status_query("draft something nice and send it out to my old customers?")
        != "last_campaign"
    )


def test_vt666_send_status_asks_still_answer() -> None:
    # The genuine send-STATUS asks keep routing (markers present) — regression floor for the gate.
    assert sq.classify_status_query("did you send it?") == "last_campaign"
    assert sq.classify_status_query("already sent?") == "last_campaign"
    assert sq.classify_status_query("has the diwali message gone out?") == "last_campaign"
    # NOTE: "bheja kya message?" routes 'unknown' — the send-CUE set never contained the Hinglish
    # "bheja" (pre-existing, unchanged by the VT-666 gate; the brain answers it). Hinglish
    # send-status cue coverage is VT-663 P2 scope, per CL-2026-07-15-no-lists (LLM-primary).
    assert sq.classify_status_query("bheja kya message?") == "unknown"
