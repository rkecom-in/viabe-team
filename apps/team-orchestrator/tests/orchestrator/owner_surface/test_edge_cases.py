"""VT-84 PR-1 — edge-case router + exclusion + status_query.

Pure tests (no DB/Anthropic): query-type classify, phone/name extraction, the router's
routing decision (injected classify_fn + monkeypatched handlers). DB integration (gated
on DATABASE_URL): exclusion incl. the consumer-opt-out PRECEDENCE, status counts,
cross-tenant. Heavy imports are local (dep-less smoke safe).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

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
    monkeypatch.setattr(
        r, "_send_edge_ack", lambda tid, phone, text: calls.update(sent=text)
    )
    monkeypatch.setattr(
        "orchestrator.owner_inputs.exclusion.handle_exclusion",
        lambda tid, body: SimpleNamespace(action="excluded", customer_id=uuid4(), response_text="ok"),
    )
    ev = SimpleNamespace(body="exclude 9876543210", sender_phone="+910000000000")
    out = r.route_edge_case(
        tenant_id="t", event=ev, classify_fn=lambda b: SimpleNamespace(classification="exclusion_request")
    )
    assert out is not None and "edge_case:exclusion" in out.reason
    assert calls.get("sent") == "ok"


def test_router_routes_status(monkeypatch) -> None:
    import orchestrator.edge_cases_router as r

    monkeypatch.setattr(r, "_send_edge_ack", lambda tid, phone, text: None)
    monkeypatch.setattr(
        "orchestrator.owner_inputs.status_query.answer_status_query",
        lambda tid, body: "you have 42 customers",
    )
    ev = SimpleNamespace(body="how many customers", sender_phone="+910000000000")
    out = r.route_edge_case(
        tenant_id="t", event=ev, classify_fn=lambda b: SimpleNamespace(classification="status_query")
    )
    assert out is not None and out.reason == "edge_case:status_query"


@pytest.mark.parametrize("intent", ["approval", "rejection", "question", "feedback", "other"])
def test_router_falls_through(intent) -> None:
    import orchestrator.edge_cases_router as r

    # These intents fall through to the agent (None). adhoc -> "owner_initiated" marker +
    # template_error -> DispatchResult are PR-2 (tested in test_edge_cases_pr2.py).
    ev = SimpleNamespace(body="x", sender_phone=None)
    assert r.route_edge_case(tenant_id="t", event=ev, classify_fn=lambda b: SimpleNamespace(classification=intent)) is None


# ----------------------------- DB integration ------------------------------------------
@pytest.fixture
def _dbpool():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL not set; integration test requires real DB")
    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            db_url, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return get_pool()


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
    _seed(_dbpool, tid, [("Rajesh Kumar", "+919811111112", "subscribed"),
                         ("Rajesh Singh", "+919811111113", "subscribed")])
    res = handle_exclusion(tid, "don't message Rajesh")
    assert res.action == "ambiguous"  # 2 fuzzy matches -> never auto-pick
    # neither was excluded
    assert _status_of(_dbpool, tid, "+919811111112") == "subscribed"


@pytest.mark.integration
def test_status_counts(_dbpool) -> None:
    from orchestrator.owner_inputs.status_query import answer_status_query

    tid = uuid4()
    _seed(_dbpool, tid, [("A", "+919800000001", "subscribed"),
                         ("B", "+919800000002", "opted_out"),
                         ("C", "+919800000003", "owner_excluded")])
    assert "3 customers" in answer_status_query(tid, "how many customers")
    # opt_out_count = opted_out + owner_excluded = 2
    assert "2 customers are excluded" in answer_status_query(tid, "how many opt-outs?")


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
