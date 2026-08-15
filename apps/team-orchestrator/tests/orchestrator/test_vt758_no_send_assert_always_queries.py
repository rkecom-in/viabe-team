"""VT-758 — a no-send safety assert must QUERY, never default to zero.

THE DEFECT. `assert_side_effects` read:

    n = 0
    if campaign_id is not None:
        ... count campaign_messages ...
    if expect_sent_count is not None and n != expect_sent_count:

So with no campaign attributable to the turn, `expect_sent_count: 0` held trivially — the safety line
reported PASS **without touching the database**, and a vacuous pass looks exactly like a real one in the
report (no failure line, no marker).

Measured in gate (d): 1 of 10 scenarios declaring `expect_sent_count: 0` passed that way, and it was
`sr_l1_draft_only_no_autosend` — the vacuous pass landed on the run where the upstream work had already
failed, which is precisely the run where an unexpected send would be least expected and most damaging. That
is why incidence 1 does not make this minor: **the check stops checking exactly when the system is already
off-nominal.**

VT-738's row predicted this class ("masks safety assertions: every money-adjacent scenario that blocked in
the gate did so UPSTREAM of its safety check") without locating the mechanism. This is the mechanism.

The fix keeps the precise question where it can be asked (campaign-scoped) and falls back to the safe one
(tenant-wide, time-fenced to the tenant's own lifetime per VT-682) where it cannot. **"I could not find a
campaign" must never satisfy "nothing was sent".**
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

import psycopg  # noqa: E402

_CANARIES = __import__("pathlib").Path(__file__).resolve().parents[2] / "canaries"
import sys  # noqa: E402

sys.path.insert(0, str(_CANARIES))
import convo_harness as ch  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set — VT-758 tests skipped"
)


@pytest.fixture(scope="module")
def dsn():
    import apply_migrations

    url = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=url)
    assert not r["failed"], r["failed"]
    return url


def _tenant(conn) -> str:
    return str(
        conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('vt758 send-assert', 'founding', 'trial') RETURNING id"
        ).fetchone()["id"]
    )


def _send(conn, tenant_id: str, *, campaign_id: str | None, status: str = "template_sent") -> None:
    """A campaign_messages row exactly as the send path writes one (idempotency_key convention)."""
    key = f"{campaign_id}:{uuid4()}" if campaign_id else f"unkeyed:{uuid4()}"
    conn.execute(
        "INSERT INTO campaign_messages (tenant_id, send_status, idempotency_key) "
        "VALUES (%s, %s, %s)",
        (tenant_id, status, key),
    )


@pytest.mark.integration
def test_no_campaign_and_NO_send_still_passes(dsn, _dbpool):
    """The legitimate case must stay legitimate: nothing drafted, nothing sent, assert passes — but now
    on evidence rather than on a default."""
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tenant_id = _tenant(conn)
        failures = ch.assert_side_effects(conn, tenant_id, None, expect_sent_count=0)
    assert failures == [], f"a genuinely-no-send tenant must pass: {failures}"


@pytest.mark.integration
def test_THE_DEFECT_a_send_with_no_attributable_campaign_is_now_DETECTED(dsn, _dbpool):
    """THE WHOLE ROW. A real send exists, no campaign is attributable to the turn, and the scenario
    asserts `expect_sent_count: 0`.

    BEFORE: `n` defaulted to 0, the assert passed, and a send went unnoticed on a money-path scenario.
    AFTER: the tenant-wide count finds it and the assert fails.
    """
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tenant_id = _tenant(conn)
        _send(conn, tenant_id, campaign_id=None)   # a send the campaign join could never see
        failures = ch.assert_side_effects(conn, tenant_id, None, expect_sent_count=0)

    assert failures, (
        "a real send went undetected because no campaign was attributable — this is the vacuous pass "
        "VT-758 exists to remove"
    )
    assert "found 1" in failures[0]
    assert "TENANT-WIDE" in failures[0], (
        "the failure must NAME the scope: 'found 0' campaign-scoped and 'found 0' tenant-wide are "
        "different claims, and a money-path triage needs to know which one it is reading"
    )


@pytest.mark.integration
def test_a_campaign_scoped_count_still_wins_when_a_campaign_IS_attributable(dsn, _dbpool):
    """The precise question stays precise. With a campaign in hand the count is scoped to it, so a send
    belonging to some OTHER campaign does not contaminate this turn's verdict."""
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tenant_id = _tenant(conn)
        run_id = str(
            conn.execute(
                "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
                "VALUES (%s, 'webhook_pipeline_run', 'completed') RETURNING id",
                (tenant_id,),
            ).fetchone()["id"]
        )
        cid = str(
            conn.execute(
                "INSERT INTO campaigns (tenant_id, run_id, status, generated_at, plan_json) "
                "VALUES (%s, %s, 'proposed', now(), '{}'::jsonb) RETURNING id",
                (tenant_id, run_id),
            ).fetchone()["id"]
        )
        _send(conn, tenant_id, campaign_id=str(uuid4()))  # a DIFFERENT campaign's send
        failures = ch.assert_side_effects(conn, tenant_id, run_id, expect_sent_count=0)

    assert failures == [], (
        "a send keyed to a different campaign must not fail THIS turn's campaign-scoped assert — "
        f"got {failures}"
    )
    assert cid  # the campaign exists, so the scoped branch was the one taken


@pytest.mark.integration
def test_the_tenant_wide_fallback_is_time_fenced_against_dirty_residue(dsn, _dbpool):
    """VT-682 residue must not read as this turn's send.

    `--dirty` seeds accumulated state BACKDATED well before the tenant row. Counting it tenant-wide would
    swap the vacuous pass for a false failure, which is not an improvement — so the fallback only sees
    rows at/after the tenant's own `created_at`.
    """
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tenant_id = _tenant(conn)
        _send(conn, tenant_id, campaign_id=None)
        conn.execute(
            "UPDATE campaign_messages SET created_at = now() - interval '14 days' "
            "WHERE tenant_id = %s",
            (tenant_id,),
        )
        failures = ch.assert_side_effects(conn, tenant_id, None, expect_sent_count=0)

    assert failures == [], (
        "backdated dirty-seed residue was counted as this turn's send — that trades a false pass for a "
        f"false failure: {failures}"
    )
