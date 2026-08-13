"""VT-740 — the crashed-campaign terminalizer, proved by EXECUTION, not by reading.

Two reverted attempts at this row were both caught by reading rather than by a test, and the
second one shipped a query that raised ``ProgrammingError`` on every call. So this file does two
things the reverted attempts did not:

1. It PARSES both shared-fragment queries through psycopg's own converter, in BOTH the
   params-carrying and the no-params mode, and asserts the ``LIKE '…%'`` form that caused the
   revert actually fails — so the trap is documented by a failing assertion, not a comment.
2. It RUNS the sweep against a real database and asserts on the resulting rows.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

import psycopg  # noqa: E402
from psycopg._queries import PostgresQuery  # noqa: E402
from psycopg._transformer import Transformer  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from orchestrator import orphan_reaper  # noqa: E402


# --------------------------------------------------------------------------------------------
# 1. The psycopg % trap — no database needed.
# --------------------------------------------------------------------------------------------


def _parse(query: str, params) -> str:
    """Run psycopg's OWN client-side query converter — the exact step that raised on every call
    in the reverted second attempt, reached before the statement ever leaves the process."""
    try:
        PostgresQuery(Transformer()).convert(query, params)
    except Exception as exc:  # noqa: BLE001 — the failure mode IS the assertion
        return f"{type(exc).__name__}"
    return "OK"


def test_like_percent_fragment_is_a_real_trap_not_a_style_preference():
    """The reverted attempt's fragment: parses with no params, RAISES with params. One shared
    constant, two callers, one of them dead on every call."""
    like_frag = (
        "SELECT 1 FROM campaign_messages m JOIN campaigns c ON TRUE "
        "WHERE m.idempotency_key LIKE c.id::text || ':%'"
    )
    assert _parse(like_frag, None) == "OK"
    assert _parse(like_frag + " AND m.tenant_id = %(t)s", {"t": "x"}) == "ProgrammingError"


def test_doubling_the_percent_does_not_fix_it_either():
    """``%%`` parses in both modes — and is WRONG in the no-params mode: psycopg passes the string
    through verbatim when ``params is None``, so the server receives a literal ``%%`` and LIKE
    matches a percent character instead of a prefix. Silent zero rows: the inert-gate class that
    caused the FIRST revert. Asserted on the converted bytes, not on parse success."""
    esc = (
        "SELECT 1 FROM campaign_messages m JOIN campaigns c ON TRUE "
        "WHERE m.idempotency_key LIKE c.id::text || ':%%'"
    )
    pq = PostgresQuery(Transformer())
    pq.convert(esc, None)
    assert b"':%%'" in pq.query, "psycopg did NOT collapse %% when params is None"


def test_both_sweep_queries_parse_in_both_param_modes():
    """The shared fragment the sweep actually ships uses ``starts_with`` and therefore carries no
    ``%`` at all — correct whether or not the caller passes params. Both live queries are checked,
    each in both modes, because "the gate was live and the safety valve was dead" is exactly what
    a one-mode check misses."""
    assert "%" not in orphan_reaper._CAMPAIGN_MSG_LINK
    assert "starts_with(" in orphan_reaper._CAMPAIGN_MSG_LINK
    for sql in (
        orphan_reaper._CRASHED_CAMPAIGN_CANDIDATES_SQL,
        orphan_reaper._TERMINALIZE_CRASHED_CAMPAIGN_SQL,
    ):
        assert _parse(sql, {
            "delivered": ["sent"], "age_hours": 2, "limit": 10, "terminal": "failed",
            "tenant": str(uuid4()), "campaign": str(uuid4()),
            "observed_last": datetime.now(timezone.utc),
        }) == "OK"
        assert _parse(sql, None) == "OK"


# --------------------------------------------------------------------------------------------
# 2. The sweep against a real database.
# --------------------------------------------------------------------------------------------

pytestmark_realdb = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-740 crashed-campaign real-DB tests skipped",
)


class _ServicePool:
    """The shape ``orphan_reaper._service_pool`` needs: ``.connection()`` yielding a service-role
    (RLS-bypassing) connection with the production ``dict_row`` factory."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @contextmanager
    def connection(self):
        with psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row) as conn:
            yield conn


@pytest.fixture(scope="module")
def dsn():
    import apply_migrations

    d = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=d)
    assert not r["failed"], r["failed"]
    return d


@pytest.fixture()
def captured_alerts(monkeypatch):
    """Capture ``dispatch_alert`` calls instead of writing rows + firing Telegram."""
    from orchestrator.alerts import dispatch as dispatch_mod

    seen: list = []
    monkeypatch.setattr(dispatch_mod, "dispatch_alert", lambda t: seen.append(t) or uuid4())
    return seen


@pytest.fixture()
def pool(dsn, captured_alerts):
    """The sweep is CROSS-TENANT by design, so a sibling test's stranded campaign is a candidate
    for this one. Drain the database once up front so each test's assertions are about the rows it
    seeded itself — and assert per-campaign below rather than on a global count."""
    p = _ServicePool(dsn)
    orphan_reaper.reap_crashed_campaigns(pool=p)
    captured_alerts.clear()
    return p


class _CampaignAlertView:
    """One campaign's slice of a per-tenant aggregate alert, shaped like the old per-campaign one.

    Alerts aggregate PER TENANT, not per campaign: `alerts.dispatch._dedup_key` is
    `tenant_id:trigger_kind` on a 5-minute window and is not campaign-scoped, while the sweep
    terminalizes up to 200 campaigns per tick — so a per-campaign alert loop fired once and had
    every other alert silently deduped away, permanently (the terminal status IS the idempotency
    key, so those campaigns are never candidates again). This view keeps the per-campaign
    assertions readable against the aggregate payload.
    """

    def __init__(self, alert, entry: dict) -> None:  # noqa: ANN001
        self.trigger_kind = alert.trigger_kind
        self.severity = alert.severity
        self.message_text = alert.message_text
        self.payload = entry


def _alert_for(alerts: list, campaign: UUID):
    """The campaign's entry inside its tenant's aggregate alert, or None."""
    hits = []
    for a in alerts:
        for entry in a.payload.get("campaigns", []):
            if entry.get("campaign_id") == str(campaign):
                hits.append(_CampaignAlertView(a, entry))
    assert len(hits) <= 1, f"campaign {campaign} alerted {len(hits)} times"
    return hits[0] if hits else None


def _seed_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "whatsapp_number) VALUES ('VT-740 sweep', 'founding', 'trial', now(), %s) RETURNING id",
            (f"+9198{uuid4().int % 10**8:08d}",),
        ).fetchone()
    return UUID(str(row[0]))


def _seed_campaign(dsn: str, tenant: UUID, *, status: str) -> UUID:
    run_id, campaign_id = uuid4(), uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status, started_at, step_count) "
            "VALUES (%s, %s, 'completed', now(), 0)",
            (str(run_id), str(tenant)),
        )
        conn.execute(
            "INSERT INTO campaigns (id, tenant_id, run_id, status, generated_at, plan_json) "
            "VALUES (%s, %s, %s, %s, now(), '{}'::jsonb)",
            (str(campaign_id), str(tenant), str(run_id), status),
        )
    return campaign_id


def _seed_recipients(dsn: str, tenant: UUID, campaign: UUID, n: int) -> list[UUID]:
    ids = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        for i in range(n):
            cid = uuid4()
            conn.execute(
                "INSERT INTO customers (id, tenant_id, display_name) VALUES (%s, %s, %s)",
                (str(cid), str(tenant), f"c{i}"),
            )
            conn.execute(
                "INSERT INTO campaign_recipients (campaign_id, customer_id, tenant_id) "
                "VALUES (%s, %s, %s)",
                (str(campaign), str(cid), str(tenant)),
            )
            ids.append(cid)
    return ids


def _seed_messages(
    dsn: str, tenant: UUID, campaign: UUID, customers: list[UUID], *,
    send_status: str, age_hours: float, set_campaign_id: bool = True,
) -> None:
    """One ledger row per customer, backdated. ``set_campaign_id=False`` reproduces a PRE-VT-740
    row (campaign_id NULL), which the sweep must still attribute via the idempotency prefix."""
    created = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    with psycopg.connect(dsn, autocommit=True) as conn:
        for cid in customers:
            conn.execute(
                "INSERT INTO campaign_messages (tenant_id, customer_id, campaign_id, "
                "  idempotency_key, send_status, message_type, created_at) "
                "VALUES (%s, %s, %s, %s, %s, 'template', %s)",
                (
                    str(tenant), str(cid), str(campaign) if set_campaign_id else None,
                    f"{campaign}:{cid}", send_status, created,
                ),
            )


def _status(dsn: str, campaign: UUID) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute("SELECT status FROM campaigns WHERE id = %s", (str(campaign),)).fetchone()
    return str(row[0])


@pytestmark_realdb
def test_partially_sent_crashed_campaign_is_terminalized_and_alerts(dsn, pool, captured_alerts):
    """40 of 100 messaged, executor dead: the campaign leaves 'approved' (so any effect-state
    condition on it can CLEAR) and a human is paged with the real remainder."""
    tenant = _seed_tenant(dsn)
    campaign = _seed_campaign(dsn, tenant, status="approved")
    customers = _seed_recipients(dsn, tenant, campaign, 10)
    _seed_messages(dsn, tenant, campaign, customers[:4], send_status="template_sent", age_hours=5)

    assert orphan_reaper.reap_crashed_campaigns(pool=pool) == 1
    assert _status(dsn, campaign) == "failed"

    alert = _alert_for(captured_alerts, campaign)
    assert alert is not None
    assert alert.trigger_kind == "escalation"       # real people were messaged -> a human decides
    assert alert.severity == "critical"
    assert alert.payload["delivered"] == 4
    assert alert.payload["intended"] == 10
    assert alert.payload["remainder"] == 6
    assert alert.payload["campaign_id"] == str(campaign)
    # CL-390: no customer id may ride the alert text.
    assert all(str(c) not in alert.message_text for c in customers)


@pytestmark_realdb
def test_pre_vt740_rows_with_null_campaign_id_are_still_attributed(dsn, pool, captured_alerts):
    """The whole first revert was a join that matched zero rows for every campaign. The legacy
    arm must find sends written before ``campaign_id`` was populated."""
    tenant = _seed_tenant(dsn)
    campaign = _seed_campaign(dsn, tenant, status="approved")
    customers = _seed_recipients(dsn, tenant, campaign, 6)
    _seed_messages(
        dsn, tenant, campaign, customers[:3], send_status="sent", age_hours=5,
        set_campaign_id=False,
    )

    assert orphan_reaper.reap_crashed_campaigns(pool=pool) == 1
    assert _alert_for(captured_alerts, campaign).payload["delivered"] == 3, (
        "the legacy prefix arm found nothing"
    )


@pytestmark_realdb
def test_a_campaign_that_delivered_nothing_alerts_as_a_warning_not_a_page(dsn, pool, captured_alerts):
    tenant = _seed_tenant(dsn)
    campaign = _seed_campaign(dsn, tenant, status="approved")
    customers = _seed_recipients(dsn, tenant, campaign, 3)
    _seed_messages(dsn, tenant, campaign, customers, send_status="window_closed", age_hours=5)

    assert orphan_reaper.reap_crashed_campaigns(pool=pool) == 1
    alert = _alert_for(captured_alerts, campaign)
    assert alert.trigger_kind == "silent_terminal"
    assert alert.severity == "warning"
    assert alert.payload["delivered"] == 0
    assert alert.payload["attempted_not_delivered"] == 3


@pytestmark_realdb
def test_a_progressing_fanout_is_never_terminalized(dsn, pool, captured_alerts):
    """A recent ledger row means the loop is alive. This is the guard that separates this sweep
    from a mechanism that kills live campaigns."""
    tenant = _seed_tenant(dsn)
    campaign = _seed_campaign(dsn, tenant, status="approved")
    customers = _seed_recipients(dsn, tenant, campaign, 5)
    _seed_messages(dsn, tenant, campaign, customers[:2], send_status="sent", age_hours=0.01)

    assert orphan_reaper.reap_crashed_campaigns(pool=pool) == 0
    assert _status(dsn, campaign) == "approved"
    assert _alert_for(captured_alerts, campaign) is None


@pytestmark_realdb
def test_an_approved_campaign_that_never_started_is_left_alone(dsn, pool, captured_alerts):
    """No ledger row = the fan-out never began. There is no effect to contain and no evidence the
    executor is dead, so the sweep must not invent a terminal for it."""
    tenant = _seed_tenant(dsn)
    campaign = _seed_campaign(dsn, tenant, status="approved")
    _seed_recipients(dsn, tenant, campaign, 4)

    assert orphan_reaper.reap_crashed_campaigns(pool=pool) == 0
    assert _status(dsn, campaign) == "approved"
    assert _alert_for(captured_alerts, campaign) is None


@pytestmark_realdb
def test_already_terminal_campaigns_are_not_touched(dsn, pool, captured_alerts):
    tenant = _seed_tenant(dsn)
    seeded = {}
    for status in ("sent", "cancelled", "failed", "rejected", "proposed"):
        campaign = _seed_campaign(dsn, tenant, status=status)
        customers = _seed_recipients(dsn, tenant, campaign, 2)
        _seed_messages(dsn, tenant, campaign, customers, send_status="sent", age_hours=9)
        seeded[status] = campaign

    assert orphan_reaper.reap_crashed_campaigns(pool=pool) == 0
    for status, campaign in seeded.items():
        assert _status(dsn, campaign) == status
        assert _alert_for(captured_alerts, campaign) is None


@pytestmark_realdb
def test_the_sweep_is_idempotent(dsn, pool, captured_alerts):
    """The terminal status is itself the idempotency key — a second pass must find nothing, or the
    hourly schedule would re-alert forever."""
    tenant = _seed_tenant(dsn)
    campaign = _seed_campaign(dsn, tenant, status="approved")
    customers = _seed_recipients(dsn, tenant, campaign, 4)
    _seed_messages(dsn, tenant, campaign, customers[:1], send_status="sent", age_hours=5)

    assert orphan_reaper.reap_crashed_campaigns(pool=pool) == 1
    captured_alerts.clear()
    assert orphan_reaper.reap_crashed_campaigns(pool=pool) == 0
    assert _alert_for(captured_alerts, campaign) is None


@pytestmark_realdb
def test_the_write_time_race_guard_refuses_a_campaign_that_moved(dsn, pool):
    """The candidate SELECT and the UPDATE are separate statements. If the fan-out wrote another
    ledger row in between, the UPDATE must find zero rows — executed here directly with a stale
    ``observed_last``, which is exactly the state that race produces."""
    tenant = _seed_tenant(dsn)
    campaign = _seed_campaign(dsn, tenant, status="approved")
    customers = _seed_recipients(dsn, tenant, campaign, 4)
    _seed_messages(dsn, tenant, campaign, customers[:2], send_status="sent", age_hours=5)

    stale = datetime.now(timezone.utc) - timedelta(hours=9)  # older than the newest ledger row
    params = {
        "terminal": "failed", "tenant": str(tenant), "campaign": str(campaign),
        "observed_last": stale,
    }
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        assert conn.execute(
            orphan_reaper._TERMINALIZE_CRASHED_CAMPAIGN_SQL, params
        ).fetchall() == []
    assert _status(dsn, campaign) == "approved"

    # Same statement with the CORRECT observed instant does terminalize — proving the refusal
    # above came from the race guard and not from a query that never matches anything.
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        observed = conn.execute(
            "SELECT max(created_at) AS t FROM campaign_messages WHERE campaign_id = %s",
            (str(campaign),),
        ).fetchone()["t"]
        assert conn.execute(
            orphan_reaper._TERMINALIZE_CRASHED_CAMPAIGN_SQL, {**params, "observed_last": observed}
        ).fetchall() != []
    assert _status(dsn, campaign) == "failed"


@pytestmark_realdb
def test_one_tenants_sweep_never_reads_across_the_tenant_boundary(dsn, pool, captured_alerts):
    """Two tenants, same-shaped crashed campaigns: each alert must carry its OWN tenant and its
    OWN counts. A mis-joined sweep would attribute one tenant's sends to another — on this path
    that means telling a VTR the wrong customers were messaged."""
    t1, t2 = _seed_tenant(dsn), _seed_tenant(dsn)
    c1, c2 = _seed_campaign(dsn, t1, status="approved"), _seed_campaign(dsn, t2, status="approved")
    r1, r2 = _seed_recipients(dsn, t1, c1, 5), _seed_recipients(dsn, t2, c2, 5)
    _seed_messages(dsn, t1, c1, r1[:1], send_status="sent", age_hours=5)
    _seed_messages(dsn, t2, c2, r2[:4], send_status="sent", age_hours=5)

    assert orphan_reaper.reap_crashed_campaigns(pool=pool) == 2
    # One AGGREGATE alert per tenant (the dedup key is tenant:kind), each carrying only its own
    # tenant's campaigns — a mis-joined sweep would put one tenant's counts in the other's alert.
    by_tenant = {str(a.tenant_id): a.payload for a in captured_alerts}
    assert set(by_tenant) == {str(t1), str(t2)}
    assert [c["delivered"] for c in by_tenant[str(t1)]["campaigns"]] == [1]
    assert [c["delivered"] for c in by_tenant[str(t2)]["campaigns"]] == [4]
    assert by_tenant[str(t1)]["campaign_count"] == 1
    assert str(c2) not in by_tenant[str(t1)]["campaigns"][0]["campaign_id"]


@pytestmark_realdb
def test_terminalizing_contains_the_campaign_without_hiding_its_remainder(dsn, pool):
    """The point of the terminal is that the blocked CONDITION clears — not that the campaign
    disappears. Read the effect-state through the SAME wrapper method ``prod_workflow_diagnosis``
    uses, on a real RLS-scoped app_role connection, and assert the terminalized campaign is still
    there with its true intended/delivered split. If the rollup ever gained a status predicate this
    sweep would silently erase the remainder it exists to surface."""
    from orchestrator.db.wrappers import CampaignsWrapper
    from orchestrator.prod_workflow_diagnosis import _DELIVERED, EffectState

    tenant = _seed_tenant(dsn)
    campaign = _seed_campaign(dsn, tenant, status="approved")
    customers = _seed_recipients(dsn, tenant, campaign, 9)
    _seed_messages(dsn, tenant, campaign, customers[:2], send_status="sent", age_hours=5)

    assert orphan_reaper.reap_crashed_campaigns(pool=pool) == 1
    assert _status(dsn, campaign) == "failed"

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SET ROLE app_role")
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tenant),))
        rows = CampaignsWrapper().effect_state_rollup(
            tenant, delivered_statuses=_DELIVERED, conn=conn,
        )
    row = next(r for r in rows if str(r["campaign_id"]) == str(campaign))
    effect = EffectState(
        campaign_id=str(campaign), intended=int(row["intended"]), delivered=int(row["delivered"]),
        attempted_not_delivered=int(row["attempted"]),
        unattributable_delivered=int(row["unattributable_delivered"]),
    )
    assert effect.kind == "partial_send"
    assert effect.remainder == 7, "the un-messaged remainder vanished from the VTR's own read"


@pytestmark_realdb
def test_the_sweep_runs_on_the_registered_reaper_schedule(dsn, pool, captured_alerts):
    """REACHABILITY. A sweep nobody calls is inert — the failure this row has already produced
    twice. ``reap_stalled_manager_tasks`` is registered on STALLED_TASK_SWEEP_CRON, so proving the
    campaign sweep runs from inside it proves it runs at all."""
    tenant = _seed_tenant(dsn)
    campaign = _seed_campaign(dsn, tenant, status="approved")
    customers = _seed_recipients(dsn, tenant, campaign, 3)
    _seed_messages(dsn, tenant, campaign, customers[:1], send_status="sent", age_hours=5)

    orphan_reaper.reap_stalled_manager_tasks(pool=pool)
    assert _status(dsn, campaign) == "failed"
    assert _alert_for(captured_alerts, campaign) is not None


@pytestmark_realdb
def test_a_failing_ladder_still_terminalizes_crashed_campaigns(dsn, pool, captured_alerts, monkeypatch):
    """The two sweeps are independent: a retry-ladder failure must not leave a crashed campaign
    stranded 'approved' forever (that stranding is the root cause this row exists to remove)."""
    tenant = _seed_tenant(dsn)
    campaign = _seed_campaign(dsn, tenant, status="approved")
    customers = _seed_recipients(dsn, tenant, campaign, 3)
    _seed_messages(dsn, tenant, campaign, customers[:1], send_status="sent", age_hours=5)

    from orchestrator.manager import task_retry

    # Break the ladder at its FIRST statement inside the try, so the failure is unconditional and
    # does not depend on there being a stalled task to trip over.
    monkeypatch.delattr(task_retry, "decide_retry")
    assert orphan_reaper.reap_stalled_manager_tasks(pool=pool) == 0  # the ladder did fail
    assert _status(dsn, campaign) == "failed"                        # the campaign still moved
    assert _alert_for(captured_alerts, campaign) is not None
