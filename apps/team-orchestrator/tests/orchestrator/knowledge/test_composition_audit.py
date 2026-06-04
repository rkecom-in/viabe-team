"""VT-71 — composition layer audit canary (live PG).

Composition LIVES in build_sales_recovery_context (the one composition function,
Pillar 8 — not a forked module). This canary proves the VT-71 net-add:
  * one composition_audits row per compose (Pillar-7 traceability),
  * the moat layers (L3/L4) SURVIVE truncation under a large L2 (Cowork guardrail
    — the order protects the moat, doesn't starve it),
  * cross-layer dedup (an L4 doc tagged with a live L3 cohort_key is dropped),
  * cross-tenant isolation + reproducibility.

Builders are monkeypatched (no seeding) so the test drives composition + the REAL
audit write deterministically. CL-422 synthetic.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("pydantic")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — composition audit tests skipped",
)


@pytest.fixture(scope="module")
def pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, 'comp audit', 'founding', 'paid_active')",
            (tid,),
        )
    return tid


def _stub_builders(monkeypatch, *, l3_patterns, l4_skills, ledger=None):
    import orchestrator.context_builder as cb

    monkeypatch.setattr(cb, "_build_recent_campaigns", lambda tid: ([], False))
    monkeypatch.setattr(cb, "_build_pending_owner_inputs", lambda tid: ([], False))
    monkeypatch.setattr(
        cb, "_build_ledger_summary",
        lambda tid: (ledger or cb.LedgerSummary(), True),
    )
    monkeypatch.setattr(
        cb, "_build_l3_priors",
        lambda tid, rid: (cb.L3Priors(available=bool(l3_patterns), patterns=l3_patterns), bool(l3_patterns)),
    )
    monkeypatch.setattr(
        cb, "_build_l4_skills",
        lambda tid, req: (cb.L4Skills(available=bool(l4_skills), skills=l4_skills), bool(l4_skills)),
    )


def _audit_rows(pool, run_id: str) -> list[dict]:
    with pool.connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM composition_audits WHERE run_id = %s", (run_id,)
        ).fetchall()]


def _skill(title, tags, doc_id=None):
    return {"id": str(doc_id or uuid4()), "title": title, "tags": tags,
            "priority": 3, "score": 0.9, "excerpt": f"{title} excerpt"}


def test_audit_row_written_per_compose(pool, monkeypatch):
    from orchestrator.context_builder import build_sales_recovery_context

    tid, rid = _tenant(pool), str(uuid4())
    l4 = [_skill("Cafe re-engagement", ["cafe"])]
    _stub_builders(monkeypatch, l3_patterns=[{"cohort_key": "cafe|tier_2|60_90d", "n_tenants": 12}], l4_skills=l4)
    build_sales_recovery_context(UUID(tid), UUID(rid), "weekly_cadence", "re-engage cafe dormants")

    rows = _audit_rows(pool, rid)
    assert len(rows) == 1
    a = rows[0]
    assert a["total_token_count"] > 0
    assert set(a["section_token_counts"]) >= {"l3_priors", "l4_skills", "customer_ledger_summary"}
    assert a["l3_cohort_keys"] == ["cafe|tier_2|60_90d"]
    assert [str(x) for x in a["l4_doc_ids"]] == [l4[0]["id"]]


def test_moat_survives_truncation_under_large_per_tenant_section(pool, monkeypatch):
    """Cowork guardrail: a large PER-TENANT section (owner inputs) truncates the
    per-tenant plane, NOT the moat — L3/L4 survive; audit records the per-tenant
    section trimmed.

    VT-312: the ledger summary is now a fixed-size distribution (8 ints +
    business_type) — it is NOT a growable list and can no longer be the
    overflow driver (the old ``top_spenders`` list is gone). The moat-survival
    guardrail is identical; the overflow is now driven through
    ``pending_owner_inputs`` (a real growable per-tenant section the truncation
    loop trims first), proving the moat is still protected last.
    """
    from datetime import UTC, datetime

    import orchestrator.context_builder as cb
    from orchestrator.context_builder import OwnerInput, build_sales_recovery_context

    tid, rid = _tenant(pool), str(uuid4())
    # ~each row carries a ~100-char segment; 300 rows >> the 6400-token cap.
    huge_inputs = [
        OwnerInput(
            input_id=uuid4(),
            received_at=datetime.now(UTC),
            intent="winback",
            segment="x" * 100,
        )
        for _ in range(300)
    ]
    l4 = [_skill("Festival timing", ["timing"])]
    _stub_builders(
        monkeypatch,
        l3_patterns=[{"cohort_key": "cafe|tier_2|90d_plus", "n_tenants": 11}],
        l4_skills=l4,
    )
    monkeypatch.setattr(
        cb, "_build_pending_owner_inputs", lambda tid: (huge_inputs, True)
    )
    bundle = build_sales_recovery_context(UUID(tid), UUID(rid), "weekly_cadence", "x")

    assert bundle.l3_priors.available is True   # moat survived
    assert bundle.l4_skills.available is True
    a = _audit_rows(pool, rid)[0]
    assert "pending_owner_inputs" in a["truncated_sections"]
    assert "l3_priors" not in a["truncated_sections"]
    assert "l4_skills" not in a["truncated_sections"]


def test_cross_layer_dedup_drops_l4_matching_l3_cohort(pool, monkeypatch):
    from orchestrator.context_builder import build_sales_recovery_context

    tid, rid = _tenant(pool), str(uuid4())
    ck = "cafe|tier_2|60_90d"
    redundant = _skill("Cafe 60-90d response rates", [ck])   # tagged with the live L3 cohort → dedup
    kept = _skill("Message tone", ["tone"])
    _stub_builders(monkeypatch, l3_patterns=[{"cohort_key": ck, "n_tenants": 10}],
                   l4_skills=[redundant, kept])
    bundle = build_sales_recovery_context(UUID(tid), UUID(rid), "weekly_cadence", "x")

    titles = {s["title"] for s in bundle.l4_skills.skills}
    assert "Cafe 60-90d response rates" not in titles  # deduped (L3 number supersedes)
    assert "Message tone" in titles                    # heuristic kept
    a = _audit_rows(pool, rid)[0]
    assert [str(x) for x in a["l4_doc_ids"]] == [kept["id"]]  # audit reflects what the agent saw


def test_audit_is_tenant_scoped(pool, monkeypatch):
    from orchestrator.context_builder import build_sales_recovery_context

    a_tid, rid = _tenant(pool), str(uuid4())
    b_tid = _tenant(pool)
    _stub_builders(monkeypatch, l3_patterns=[], l4_skills=[])
    build_sales_recovery_context(UUID(a_tid), UUID(rid), "weekly_cadence", "x")

    from orchestrator.db import tenant_connection

    with tenant_connection(b_tid) as conn:  # tenant B's RLS scope
        n = conn.execute(
            "SELECT count(*) AS n FROM composition_audits WHERE run_id = %s", (rid,)
        ).fetchone()
    assert int(dict(n)["n"]) == 0  # B cannot see A's audit row
