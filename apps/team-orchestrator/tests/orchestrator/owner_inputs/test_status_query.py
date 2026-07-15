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


def test_classify_bare_list_ask_vetoes_customer_count() -> None:
    # A list ask with NO inactivity cue is neither a count nor a lapsed_list -> brain (CD2 names path).
    assert sq.classify_status_query("give me a list of my customers") == "unknown"
    assert sq.classify_status_query("list all my customer names") == "unknown"
    # A plain count ask (no list cue) still routes to customer_count.
    assert sq.classify_status_query("how many customers do I have?") == "customer_count"


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


def test_lapsed_list_render_is_count_plus_offer_never_names(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw(monkeypatch, _FakeCW(with_sales=6, lapsed=5))
    ans = sq.answer_status_query(
        _TID, "kaafi customers hain jinhone 60 din se zyada order nahi kiya — unki list bhej do mujhe"
    )
    assert ans is not None
    assert "5" in ans and str(sq.LAPSED_WINDOW_DAYS) in ans
    # CD2 interim: the render OFFERS the list, it never dumps names inline.
    assert "list" in ans.lower()


def test_lapsed_list_empty_ledger_is_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw(monkeypatch, _FakeCW(with_sales=0, lapsed=0))
    ans = sq.answer_status_query(_TID, "give me a list of lapsed customers")
    assert ans is not None and "sales history" in ans.lower()


def test_lapsed_count_and_list_share_the_same_number(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw(monkeypatch, _FakeCW(with_sales=9, lapsed=3))
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
