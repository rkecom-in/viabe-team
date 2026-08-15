"""VT-563 — attribution-outcome PRODUCER end-to-end (real Postgres).

Proves the leg that was severed (empty ``attributions`` table ⇒ dead sweep ⇒
0 recovered_paise) is joined up:

  writer (build_campaign_attributions, at close)
    → attributions rows
    → attribution_close aggregate + back-annotation of the originating run
    → implicit_attribution sweep writes owner_feedback
    → context_builder recovered_paise + completeness True

Gated on DATABASE_URL + RUN_INTEGRATION_TESTS=1 (CL-422 — synthetic data only;
unique tenants per test so a recycled DB never collides). NO real phone numbers.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("psycopg")

from psycopg.types.json import Jsonb  # noqa: E402

from orchestrator.billing.attribution_close import (  # noqa: E402
    _baseline_paise,
    close_attribution,
)


@pytest.fixture(autouse=True)
def _phone_salt(monkeypatch):
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "test-salt-vt563")


# --------------------------------------------------------------------------
# Pure (no DB) — baseline extraction from plan_json.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "plan_json, expected",
    [
        ({"expected_arrr": {"low_paise": 12345}}, 12345),
        ({"expected_arrr": {"low_paise": 0}}, 0),
        ({"expected_arrr": {"low_paise": -5}}, 0),          # clamped ≥ 0
        ({"expected_arrr": {"low_paise": "900"}}, 900),     # coerced
        ({"expected_arrr": {"high_paise": 100}}, 0),        # low missing
        ({"expected_arrr": {"low_paise": None}}, 0),
        ({"expected_arrr": "not-a-dict"}, 0),
        ({}, 0),
        (None, 0),
        ("garbage", 0),
    ],
)
def test_baseline_paise(plan_json, expected) -> None:
    assert _baseline_paise(plan_json) == expected


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_tenant(pool, tid: str) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'paid_active') ON CONFLICT (id) DO NOTHING",
            (tid, f"vt563-{tid[:8]}"),
        )


def _seed_customer(pool, tid: str, cid: str) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (id, tenant_id, display_name, source) "
            "VALUES (%s, %s, %s, 'test') ON CONFLICT (id) DO NOTHING",
            (cid, tid, f"cust-{cid[:8]}"),
        )


def _seed_completed_run(pool, tid: str) -> str:
    rid = str(uuid.uuid4())
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status, started_at) "
            "VALUES (%s, %s, 'completed', now() - interval '8 days')",
            (rid, tid),
        )
    return rid


def _seed_campaign(
    pool, tid: str, run_id: str, *, close_at: datetime, baseline_low_paise: int
) -> str:
    caid = str(uuid.uuid4())
    plan = {
        "expected_arrr": {
            "low_paise": baseline_low_paise,
            "high_paise": baseline_low_paise * 10 + 1,
            "confidence": "medium",
            "basis": "vt563 synthetic",
        }
    }
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO campaigns "
            "(id, tenant_id, run_id, plan_json, status, generated_at, attribution_close_at) "
            "VALUES (%s, %s, %s, %s, 'sent', now() - interval '8 days', %s)",
            (caid, tid, run_id, Jsonb(plan), close_at),
        )
        cur.execute(
            "INSERT INTO campaign_recipients (campaign_id, customer_id, tenant_id) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (caid, _RECIPIENT[tid], tid),
        )
    return caid


def _seed_click_signal(pool, tid: str, customer_id: str, *, clicked_at) -> None:
    """VT-754 / D-C — an ATTRIBUTABLE SIGNAL. Since the ruling, a coincident sale earns nothing: the
    recipient must have clicked a tracked link (or replied) BEFORE the purchase. Seeds the
    customer_hook_links binding a real mint would write."""
    token = f"tok-{uuid.uuid4().hex[:16]}"
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hook_links (token, tenant_id, source) VALUES (%s, %s, 'test')",
            (token, tid),
        )
        cur.execute(
            "INSERT INTO customer_hook_links "
            "(tenant_id, customer_id, token, source, click_count, first_clicked_at, last_clicked_at) "
            "VALUES (%s, %s, %s, 'test', 1, %s, %s)",
            (tid, customer_id, token, clicked_at, clicked_at),
        )


def _seed_ledger_payment(
    pool, tid: str, customer_id: str, *, amount_paise: int, entry_date, entry_type: str,
    source_confidence: float = 0.9,
) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customer_ledger_entries "
            "(tenant_id, customer_id, amount_paise, entry_type, entry_date, "
            " acquired_via, source_confidence, entry_key) "
            "VALUES (%s, %s, %s, %s, %s, 'upi_gpay', %s, %s)",
            (tid, customer_id, amount_paise, entry_type, entry_date,
             source_confidence, str(uuid.uuid4())),
        )


# per-tenant recipient customer, so _seed_campaign can link without re-passing it.
_RECIPIENT: dict[str, str] = {}


# Child tables (FK → tenants) the close/drain path touches, deleted before the
# tenant. Includes the KG outbox + L1 rows the ATTRIBUTION_CREATED emit produces.
_CHILD_TABLES = (
    "owner_feedback", "attributions", "campaign_recipients",
    "customer_ledger_entries", "campaigns", "l1_relationships", "l1_entities",
    "kg_events_processed", "kg_events", "pipeline_log", "pipeline_runs",
    "customers",
)


def _cleanup(pool, tid: str) -> None:
    """Best-effort synthetic-data teardown. Unique per-test uuids mean a residual
    row never collides, so a stray FK (an untracked tenant-scoped table) is
    tolerated rather than allowed to fail the test."""
    for table in _CHILD_TABLES:
        try:
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tid,))  # noqa: S608 — fixed table list
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM tenants WHERE id = %s", (tid,))
    except Exception:  # noqa: BLE001 — residual FK from an untracked table; harmless
        pass


def _attributions(pool, campaign_id: str) -> list[dict]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT customer_id::text AS customer_id, attributed_paise, "
            "       attribution_method, attribution_confidence "
            "FROM attributions WHERE campaign_id = %s ORDER BY attributed_paise",
            (campaign_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def _run_meta(pool, run_id: str) -> dict:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT terminal_state_metadata FROM pipeline_runs WHERE id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    meta = (row["terminal_state_metadata"] if isinstance(row, dict) else row[0]) or {}
    return meta if isinstance(meta, dict) else json.loads(meta)


@pytest.mark.integration
def test_writer_produces_rows_and_filters_window_and_type(_dbpool) -> None:
    """VT-754 / D-C: one attributions row per in-window purchase THAT FOLLOWS AN ATTRIBUTABLE
    SIGNAL. Out-of-window purchases are excluded, and so is a purchase that PRECEDES the signal —
    a sale before the click is a coincidence with better timing, not a cause.

    The old version of this test seeded a bare `entry_type='payment'` and asserted it was
    attributed. That is precisely the over-claim the ruling rejects (a shop's sales continue whether
    or not we messaged anyone), and it is also why the test never caught that no producer writes
    'payment' at all — it supplied the value under test.
    """
    tid = str(uuid.uuid4())
    cust = str(uuid.uuid4())
    _RECIPIENT[tid] = cust
    close_at = _now()
    try:
        _seed_tenant(_dbpool, tid)
        _seed_customer(_dbpool, tid, cust)
        run_id = _seed_completed_run(_dbpool, tid)
        caid = _seed_campaign(_dbpool, tid, run_id, close_at=close_at, baseline_low_paise=10000)

        # The signal: the recipient clicked, 3 days before the close.
        _seed_click_signal(_dbpool, tid, cust, clicked_at=close_at - timedelta(days=3))

        # In-window purchase AFTER the click → attributed. `sale` is what every production producer
        # actually writes (ingest / upi_export / _image_adapter / imported_transactions).
        _seed_ledger_payment(_dbpool, tid, cust, amount_paise=50000,
                             entry_date=close_at.date(), entry_type="sale")
        # Out-of-window purchase (30d before close) → NOT attributed.
        _seed_ledger_payment(_dbpool, tid, cust, amount_paise=99999,
                             entry_date=(close_at - timedelta(days=30)).date(),
                             entry_type="sale")
        # In-window but BEFORE the click → NOT attributed (the purchase must follow the signal).
        _seed_ledger_payment(_dbpool, tid, cust, amount_paise=77777,
                             entry_date=(close_at - timedelta(days=5)).date(),
                             entry_type="sale")

        result = close_attribution(caid)

        rows = _attributions(_dbpool, caid)
        assert len(rows) == 1, rows
        assert rows[0]["attributed_paise"] == 50000
        assert rows[0]["attribution_method"] == "tracked_link"
        assert abs(float(rows[0]["attribution_confidence"]) - 0.9) < 1e-4
        assert rows[0]["customer_id"] == cust
        assert result.total_arrr_paise == 50000
        assert result.attribution_row_count == 1
        assert result.already_closed is False
    finally:
        _cleanup(_dbpool, tid)


@pytest.mark.integration
def test_back_annotation_and_sweep_end_to_end_thumbs_up(_dbpool) -> None:
    """close back-annotates the originating run; the implicit sweep then writes a
    thumbs_up owner_feedback row (outcome 50000 > baseline 10000)."""
    tid = str(uuid.uuid4())
    cust = str(uuid.uuid4())
    _RECIPIENT[tid] = cust
    close_at = _now()
    try:
        _seed_tenant(_dbpool, tid)
        _seed_customer(_dbpool, tid, cust)
        run_id = _seed_completed_run(_dbpool, tid)
        caid = _seed_campaign(_dbpool, tid, run_id, close_at=close_at, baseline_low_paise=10000)
        # VT-754 / D-C: a purchase earns credit only BEHIND an attributable signal.
        _seed_click_signal(_dbpool, tid, cust, clicked_at=close_at - timedelta(days=2))
        _seed_ledger_payment(_dbpool, tid, cust, amount_paise=50000,
                             entry_date=close_at.date(), entry_type="sale")

        close_attribution(caid)

        # Back-annotation: originating run carries the exact keys the sweep reads.
        meta = _run_meta(_dbpool, run_id)
        assert int(meta["attribution_outcome"]) == 50000
        assert int(meta["attribution_baseline"]) == 10000
        assert "attribution_outcome_at" in meta

        # End-to-end: the sweep now picks up this production-shaped run.
        from orchestrator.feedback.implicit_attribution import (
            run_implicit_attribution_sweep,
        )

        counts = run_implicit_attribution_sweep()
        assert counts["written"] >= 1

        with _dbpool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT signal FROM owner_feedback "
                "WHERE tenant_id = %s AND run_id = %s AND tier = 'implicit'",
                (tid, run_id),
            )
            fb = cur.fetchone()
        signal = fb["signal"] if isinstance(fb, dict) else (fb[0] if fb else None)
        assert signal == "thumbs_up"

        # Idempotent: re-running close + sweep does not double-write.
        again = close_attribution(caid)
        assert again.already_closed is True
        assert len(_attributions(_dbpool, caid)) == 1
        run_implicit_attribution_sweep()
        with _dbpool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM owner_feedback "
                "WHERE tenant_id = %s AND run_id = %s AND tier = 'implicit'",
                (tid, run_id),
            )
            r = cur.fetchone()
        assert int(r["n"] if isinstance(r, dict) else r[0]) == 1
    finally:
        _cleanup(_dbpool, tid)


@pytest.mark.integration
def test_sweep_thumbs_down_when_outcome_below_baseline(_dbpool) -> None:
    """Recovered ARRR below the campaign's own conservative prediction → thumbs_down."""
    tid = str(uuid.uuid4())
    cust = str(uuid.uuid4())
    _RECIPIENT[tid] = cust
    close_at = _now()
    try:
        _seed_tenant(_dbpool, tid)
        _seed_customer(_dbpool, tid, cust)
        run_id = _seed_completed_run(_dbpool, tid)
        caid = _seed_campaign(_dbpool, tid, run_id, close_at=close_at, baseline_low_paise=100000)
        # VT-754 / D-C: a purchase earns credit only BEHIND an attributable signal.
        _seed_click_signal(_dbpool, tid, cust, clicked_at=close_at - timedelta(days=2))
        _seed_ledger_payment(_dbpool, tid, cust, amount_paise=20000,
                             entry_date=close_at.date(), entry_type="sale")

        close_attribution(caid)
        from orchestrator.feedback.implicit_attribution import (
            run_implicit_attribution_sweep,
        )

        run_implicit_attribution_sweep()
        with _dbpool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT signal FROM owner_feedback "
                "WHERE tenant_id = %s AND run_id = %s AND tier = 'implicit'",
                (tid, run_id),
            )
            fb = cur.fetchone()
        signal = fb["signal"] if isinstance(fb, dict) else (fb[0] if fb else None)
        assert signal == "thumbs_down"
    finally:
        _cleanup(_dbpool, tid)


@pytest.mark.integration
def test_context_builder_reads_real_recovered_paise(_dbpool) -> None:
    """context_builder._build_recent_campaigns returns real recovered_paise +
    completeness True once the attribution substrate is populated."""
    tid = str(uuid.uuid4())
    cust = str(uuid.uuid4())
    _RECIPIENT[tid] = cust
    close_at = _now()
    try:
        _seed_tenant(_dbpool, tid)
        _seed_customer(_dbpool, tid, cust)
        run_id = _seed_completed_run(_dbpool, tid)
        caid = _seed_campaign(_dbpool, tid, run_id, close_at=close_at, baseline_low_paise=10000)
        # VT-754 / D-C: a purchase earns credit only BEHIND an attributable signal.
        _seed_click_signal(_dbpool, tid, cust, clicked_at=close_at - timedelta(days=2))
        _seed_ledger_payment(_dbpool, tid, cust, amount_paise=50000,
                             entry_date=close_at.date(), entry_type="sale")
        close_attribution(caid)

        from orchestrator.context_builder import _build_recent_campaigns

        snapshots, complete = _build_recent_campaigns(uuid.UUID(tid))
        assert complete is True
        by_id = {str(s.campaign_id): s for s in snapshots}
        assert str(caid) in by_id
        assert by_id[str(caid)].recovered_paise == 50000
    finally:
        _cleanup(_dbpool, tid)
