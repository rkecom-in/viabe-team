"""VT-84 PR-1 — edge-case router + exclusion + status_query.

Pure tests (no DB/Anthropic): query-type classify, phone/name extraction, the router's
routing decision (injected classify_fn + monkeypatched handlers). DB integration (gated
on DATABASE_URL): exclusion incl. the consumer-opt-out PRECEDENCE, status counts,
cross-tenant. Heavy imports are local (dep-less smoke safe).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

# Dep-less CI 'test' job: owner_inputs/__init__ -> writer -> anthropic. Skip if absent.
pytest.importorskip("anthropic")

from orchestrator.owner_inputs.exclusion import _extract_name, _extract_phone
from orchestrator.owner_inputs.status_query import classify_status_query


# ----------------------------- pure: status-query classify ----------------------------
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("how many customers do I have?", "customer_count"),
        ("kitne customers hain", "customer_count"),
        ("how many opt-outs this month?", "opt_out_count"),
        ("how many opted out?", "opt_out_count"),
        ("what was the last campaign result?", "last_campaign"),
        ("what's my trial status?", "billing"),
        ("good morning", "unknown"),
        # VT-632 lapsed_count — the "lapsed"/"dormant" TOKEN routes to lapsed_count (the dormant
        # subset), NOT customer_count (total), even though "customers" is present. Fixes the
        # sr_cohort "how many lapsed customers... in total" → total-vs-lapsed defect.
        ("and how many lapsed customers do I have in total?", "lapsed_count"),
        ("how many dormant customers?", "lapsed_count"),
        ("how many lapsed?", "lapsed_count"),
        # Guard: a plain customer count with NO lapsed/dormant token stays customer_count.
        ("how many customers do I have on file?", "customer_count"),
        # Guard: a behavioural "haven't bought" phrase (no token) is NOT lapsed_count here — it
        # stays with the brain's speech-act guard, so the deterministic parse returns unknown.
        ("how many haven't bought anything in a while?", "unknown"),
        # VT-632 finance guard: a cash-flow read falls through to the brain (not a status_query
        # qtype) — and a NEGATED 'campaigns' token in the same message must NOT hijack it.
        (
            "Just tell me roughly how my cash flow is looking this week — "
            "no drafts, no messages, no campaigns. Only the number.",
            "unknown",
        ),
        ("how is my cash flow this month?", "unknown"),
        ("what's my revenue looking like", "unknown"),
        # SEND-STATUS question → last_campaign (honest "did anything go out?"), checked BEFORE
        # customer_count so the stray "customers" token can't hijack it into a ledger count (the
        # m_honesty_fabricated_campaign non-sequitur, official §2 2026-07-10).
        ("did you already send that winback message to my old customers?", "last_campaign"),
        ("have you sent it yet?", "last_campaign"),
        ("has the message gone out to my customers?", "last_campaign"),
        ("did the winback go out?", "last_campaign"),
    ],
)
def test_classify_status_query(body, expected) -> None:
    assert classify_status_query(body) == expected


# ----------------------------- pure: phone / name extraction ---------------------------
@pytest.mark.parametrize(
    ("body", "phone"),
    [
        ("exclude customer 9876543210, he is angry", "+919876543210"),
        ("don't message +91 98765 43210 again", "+919876543210"),
        ("exclude 09876543210", "+919876543210"),
        ("don't message Rajesh again", None),
    ],
)
def test_extract_phone(body, phone) -> None:
    assert _extract_phone(body) == phone


@pytest.mark.parametrize(
    ("body", "name"),
    [
        ("don't message Rajesh again", "Rajesh"),
        ("exclude customer Priya Sharma", "Priya Sharma"),
        ("exclude customer 9876543210", None),  # only a number -> no name
    ],
)
def test_extract_name(body, name) -> None:
    assert _extract_name(body) == name


# ----------------------------- pure: router routing (mocked) ---------------------------
def test_router_routes_exclusion(monkeypatch) -> None:
    import orchestrator.edge_cases_router as r

    calls: dict[str, object] = {}
    monkeypatch.setattr(r, "_send_edge_ack", lambda tid, phone, text: calls.update(sent=text))
    monkeypatch.setattr(
        "orchestrator.owner_inputs.exclusion.handle_exclusion",
        lambda tid, body: SimpleNamespace(
            action="excluded", customer_id=uuid4(), response_text="ok"
        ),
    )
    ev = SimpleNamespace(body="exclude 9876543210", sender_phone="+910000000000")
    out = r.route_edge_case(
        tenant_id="t",
        event=ev,
        classify_fn=lambda b: SimpleNamespace(classification="exclusion_request", confidence=0.9),
    )
    assert out is not None and "edge_case:exclusion" in out.reason
    assert calls.get("sent") == "ok"


def test_exclusion_below_confidence_floor_falls_through() -> None:
    """VT-336: a LOW-confidence exclusion_request must NOT auto-exclude — it falls through to the
    agent (the mutating fast-path requires the confidence floor; a misroute lands on reasoning)."""
    import orchestrator.edge_cases_router as r

    ev = SimpleNamespace(body="maybe remove someone later?", sender_phone="+910000000000")
    out = r.route_edge_case(
        tenant_id="t",
        event=ev,
        classify_fn=lambda b: SimpleNamespace(classification="exclusion_request", confidence=0.4),
    )
    assert out is None  # below floor → fall through, no exclusion fired


def test_router_routes_status(monkeypatch) -> None:
    import orchestrator.edge_cases_router as r

    monkeypatch.setattr(r, "_send_edge_ack", lambda tid, phone, text: None)
    monkeypatch.setattr(
        "orchestrator.owner_inputs.status_query.answer_status_query",
        lambda tid, body: "you have 42 customers",
    )
    ev = SimpleNamespace(body="how many customers", sender_phone="+910000000000")
    out = r.route_edge_case(
        tenant_id="t",
        event=ev,
        classify_fn=lambda b: SimpleNamespace(classification="status_query"),
    )
    assert out is not None and out.reason == "edge_case:status_query"


@pytest.mark.parametrize(
    "intent", ["approval", "rejection", "question", "feedback", "other", "business_analysis"]
)
def test_router_falls_through(intent) -> None:
    import orchestrator.edge_cases_router as r

    # These intents fall through to the agent (None). adhoc -> "owner_initiated" marker +
    # template_error -> DispatchResult are PR-2 (tested in test_edge_cases_pr2.py).
    # business_analysis (VT-595) is deliberately NOT fast-pathed here — it belongs to the
    # Team-Manager brain, which owns delegating the analysis to the Sales-Recovery lane.
    ev = SimpleNamespace(body="x", sender_phone=None)
    assert (
        r.route_edge_case(
            tenant_id="t", event=ev, classify_fn=lambda b: SimpleNamespace(classification=intent)
        )
        is None
    )


def test_router_business_analysis_falls_through_not_status_query(monkeypatch) -> None:
    """VT-595 regression: 'which of my customers have stopped buying?' classifies as
    business_analysis, NOT status_query — it must fall through to the brain, never call
    answer_status_query, and populate intent_sink so the brain sees it as its prior."""
    import orchestrator.edge_cases_router as r

    called: dict[str, object] = {}
    monkeypatch.setattr(
        "orchestrator.owner_inputs.status_query.answer_status_query",
        lambda tid, body: called.setdefault("called", True),
    )
    sink: dict[str, object] = {}
    ev = SimpleNamespace(body="which of my customers have stopped buying?", sender_phone=None)
    out = r.route_edge_case(
        tenant_id="t",
        event=ev,
        classify_fn=lambda b: SimpleNamespace(
            classification="business_analysis",
            confidence=0.9,
            suggested_action="analyze lapsed customers via sales recovery",
        ),
        intent_sink=sink,
    )
    assert out is None  # falls through to the agent — no fast-path terminal
    assert "called" not in called  # answer_status_query never invoked
    assert sink["classification"] == "business_analysis"
    assert sink["confidence"] == pytest.approx(0.9)


def test_router_status_query_regression_still_fast_paths(monkeypatch) -> None:
    """Regression guard: a genuine pure-count status_query still fast-paths (VT-595 must not
    break the existing status_query short-circuit for real count asks)."""
    import orchestrator.edge_cases_router as r

    monkeypatch.setattr(r, "_send_edge_ack", lambda tid, phone, text: None)
    monkeypatch.setattr(
        "orchestrator.owner_inputs.status_query.answer_status_query",
        lambda tid, body: "you have 8 customers",
    )
    ev = SimpleNamespace(body="how many customers do I have?", sender_phone="+910000000000")
    out = r.route_edge_case(
        tenant_id="t",
        event=ev,
        classify_fn=lambda b: SimpleNamespace(classification="status_query", confidence=0.92),
    )
    assert out is not None and out.reason == "edge_case:status_query"


def test_router_status_query_unknown_parse_falls_through_to_brain(monkeypatch) -> None:
    """VT-600 (VT-598 opus-judge finding): the classifier tags a conversational
    confirmation ('did you get my store address?') as status_query, but the
    deterministic parse owns no such lookup — the router must fall through to
    the brain (None), never send the old canned portal deflection."""
    import orchestrator.edge_cases_router as r

    sent: list = []
    monkeypatch.setattr(r, "_send_edge_ack", lambda tid, phone, text: sent.append(text))
    ev = SimpleNamespace(body="did you get my store address?", sender_phone="+910000000000")
    out = r.route_edge_case(
        tenant_id="t",
        event=ev,
        classify_fn=lambda b: SimpleNamespace(classification="status_query", confidence=0.9),
    )
    assert out is None  # falls through to the agent
    assert sent == []  # nothing canned was sent


def test_answer_status_query_unknown_returns_none() -> None:
    """VT-600 — the parse's 'unknown' bucket returns None (no portal deflection)."""
    from uuid import uuid4

    from orchestrator.owner_inputs.status_query import answer_status_query

    assert answer_status_query(uuid4(), "did you get my store address?") is None


# ----------------------------- DB integration ------------------------------------------


def _seed(pool, tid: UUID, customers: list[tuple[str, str, str]]) -> None:
    """customers: (display_name, phone_e164, opt_out_status)."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tid), f"vt84-{tid}"),
        )
        for name, phone, status in customers:
            conn.execute(
                "INSERT INTO customers (tenant_id, display_name, phone_e164, opt_out_status) "
                "VALUES (%s, %s, %s, %s)",
                (str(tid), name, phone, status),
            )


def _status_of(pool, tid: UUID, phone: str) -> str:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT opt_out_status FROM customers WHERE tenant_id=%s AND phone_e164=%s",
            (str(tid), phone),
        ).fetchone()["opt_out_status"]


@pytest.mark.integration
def test_exclusion_phone_sets_owner_excluded(_dbpool) -> None:
    from orchestrator.owner_inputs.exclusion import handle_exclusion

    tid = uuid4()
    _seed(_dbpool, tid, [("Rajesh", "+919876543210", "subscribed")])
    res = handle_exclusion(tid, "exclude customer 9876543210, he is angry")
    assert res.action == "excluded"
    assert _status_of(_dbpool, tid, "+919876543210") == "owner_excluded"


@pytest.mark.integration
def test_exclusion_consumer_optout_precedence(_dbpool) -> None:
    """A consumer 'opted_out' is NEVER downgraded to owner_excluded (precedence)."""
    from orchestrator.owner_inputs.exclusion import handle_exclusion

    tid = uuid4()
    _seed(_dbpool, tid, [("Priya", "+919811111111", "opted_out")])
    res = handle_exclusion(tid, "exclude 9811111111")
    assert res.action == "already_excluded"  # 0 rows updated (guarded WHERE subscribed)
    assert _status_of(_dbpool, tid, "+919811111111") == "opted_out"  # UNCHANGED


@pytest.mark.integration
def test_exclusion_ambiguous_name_asks_for_phone(_dbpool) -> None:
    from orchestrator.owner_inputs.exclusion import handle_exclusion

    tid = uuid4()
    _seed(
        _dbpool,
        tid,
        [
            ("Rajesh Kumar", "+919811111112", "subscribed"),
            ("Rajesh Singh", "+919811111113", "subscribed"),
        ],
    )
    res = handle_exclusion(tid, "don't message Rajesh")
    assert res.action == "ambiguous"  # 2 fuzzy matches -> never auto-pick
    # neither was excluded
    assert _status_of(_dbpool, tid, "+919811111112") == "subscribed"


@pytest.mark.integration
def test_status_counts(_dbpool) -> None:
    from orchestrator.owner_inputs.status_query import answer_status_query

    tid = uuid4()
    _seed(
        _dbpool,
        tid,
        [
            ("A", "+919800000001", "subscribed"),
            ("B", "+919800000002", "opted_out"),
            ("C", "+919800000003", "owner_excluded"),
        ],
    )
    assert "3 customers" in answer_status_query(tid, "how many customers")
    # opt_out_count = opted_out + owner_excluded = 2
    assert "2 customers are excluded" in answer_status_query(tid, "how many opt-outs?")
    # VT-632 lapsed_count empty-ledger honesty: 3 customers seeded but NO sales -> count_with_sales=0
    # -> honest "no sales history yet", NEVER a fabricated "everyone bought within 45 days"
    # (the sr_empty_cohort_honesty regression: a 0 lapsed count must not assert a positive claim).
    _lapsed = answer_status_query(tid, "how many lapsed customers?").lower()
    assert "sales history" in _lapsed  # honest no-data path
    assert "45 days" not in _lapsed  # NOT the fabricated "everyone bought within 45 days" claim


@pytest.mark.integration
def test_exclusion_cross_tenant(_dbpool) -> None:
    from orchestrator.owner_inputs.exclusion import handle_exclusion

    a, b = uuid4(), uuid4()
    _seed(_dbpool, a, [("X", "+919700000001", "subscribed")])
    _seed(_dbpool, b, [("Y", "+919700000002", "subscribed")])
    # tenant a excludes its own number; tenant b's customer with a DIFFERENT number is untouched
    handle_exclusion(a, "exclude 9700000001")
    assert _status_of(_dbpool, a, "+919700000001") == "owner_excluded"
    assert _status_of(_dbpool, b, "+919700000002") == "subscribed"


@pytest.mark.integration
def test_exclusion_same_phone_cross_tenant(_dbpool) -> None:
    """VT-336: tenant A + B share the SAME phone; A's exclude touches ONLY A's row — proves the
    tenant-predicate (not just that unrelated rows are untouched, as the test above does)."""
    from orchestrator.owner_inputs.exclusion import handle_exclusion

    a, b = uuid4(), uuid4()
    shared = "+919700000001"
    _seed(_dbpool, a, [("A-Cust", shared, "subscribed")])
    _seed(_dbpool, b, [("B-Cust", shared, "subscribed")])
    handle_exclusion(a, "exclude 9700000001")
    assert _status_of(_dbpool, a, shared) == "owner_excluded"  # A's row excluded
    assert _status_of(_dbpool, b, shared) == "subscribed"  # B's identical-phone row UNTOUCHED


@pytest.mark.integration
def test_owner_exclude_respects_consumer_stop_consent_gate(_dbpool) -> None:
    """VT-336: a consumer STOP (record_of_consent.opted_out_at) STILL fail-closes the send after
    the owner separately excludes — the VT-45 consent gate is the real guard (a SEPARATE table
    from customers.opt_out_status). Uses the REAL consent path (the prior test wrongly used
    customers.opt_out_status='opted_out')."""
    from orchestrator.owner_inputs.exclusion import handle_exclusion
    from orchestrator.privacy.consent import (
        has_consent_for_phone,
        opt_out_for_phone,
        record_consent,
    )

    tid = uuid4()
    phone = "+919765000000"
    _seed(_dbpool, tid, [("Rajesh", phone, "subscribed")])
    record_consent(tid, phone, consent_text_version="qr_v0")
    assert has_consent_for_phone(tid, phone) is True
    opt_out_for_phone(tid, phone)  # consumer STOP → record_of_consent.opted_out_at set
    assert has_consent_for_phone(tid, phone) is False  # the consent gate now fail-closes

    res = handle_exclusion(tid, f"exclude {phone[3:]}")  # owner excludes (separate flag)
    assert res.action == "excluded"
    assert _status_of(_dbpool, tid, phone) == "owner_excluded"
    # The guarantee: the consent gate is STILL closed (owner-exclude never clears a consumer STOP).
    assert has_consent_for_phone(tid, phone) is False
