"""VT-754 under ruling D-C — attribution errs UNDER, and the ledger vocabulary cannot drift again.

TWO DEFECTS, AND ONLY THE FIRST WAS VISIBLE.

**Visible:** `attribution_writer` joined on `cle.entry_type = 'payment'` while every production
producer writes `'sale'` — `integrations/ingest.py`, `methods/upi_export.py` (changed FROM 'payment'
by VT-417), `methods/_image_adapter.py`, `imported_transactions.py` (the model default). So the
writer matched nothing and `attributions` has been empty since VT-417, which made revenue
attribution silently zero while looking implemented.

**Invisible, and worse:** correcting the vocabulary alone would have produced a WRONG NON-ZERO. A
shop's sales continue whether or not we messaged anyone, so joining recipients to any coincident sale
credits the campaign with revenue it did not cause — at scale, in an owner-facing number. VT-754
recommended exactly that (accept both values) and D-C rejected it. Recorded here because the
recommendation was wrong in an instructive way: it solved "make the join match the data" when the
question was "what did we actually cause".

**D-C: only two things count** — a tracked link/code, or a reply followed by a purchase inside a
defined window (7 days, from the existing `ATTRIBUTION_WINDOW_DAYS`; no new number enters the system).

WHY THE EXISTING TESTS NEVER CAUGHT ANY OF THIS. They inserted `entry_type='payment'` BY HAND and
asserted it was attributed — a test that supplies the value under test proves nothing about the
producers. Both halves are fixed here: this file checks readers against what producers actually
write, and drives one row through the REAL producer path.
"""

from __future__ import annotations

import ast
import os
import pathlib
import uuid

import pytest

pytest.importorskip("psycopg")

_SRC = pathlib.Path(__file__).resolve().parents[3] / "src" / "orchestrator"

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


# --- scope 3: the shared-vocabulary gate --------------------------------------------------------


def _producer_entry_types() -> set[str]:
    """Every literal a production path writes into `customer_ledger_entries.entry_type`.

    Found by reading the INSERT sites' literals rather than by grepping for the word: the point of
    this gate is that a reader and a producer disagreed for months, so it has to look at what is
    actually written.
    """
    found: set[str] = set()
    for path in _SRC.rglob("*.py"):
        text = path.read_text()
        if "customer_ledger_entries" not in text:
            continue
        if "INSERT INTO customer_ledger_entries" not in text and "entry_type" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            # `entry_type="sale"` / `entry_type: str = "sale"` / a bare "sale" beside the table name
            if isinstance(node, ast.keyword) and node.arg == "entry_type":
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    found.add(node.value.value)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "entry_type" and isinstance(node.value, ast.Constant):
                    if isinstance(node.value.value, str):
                        found.add(node.value.value)
    return found


def test_the_reader_accepts_every_value_a_producer_writes():
    """THE GATE. The defect was a two-word vocabulary mismatch between one reader and four producers,
    and nothing in the build could see it. If a producer starts writing a new entry_type, this fails
    and the reader must be reconciled deliberately — the same shape VT-746 used to close its Literal
    vs CHECK drift."""
    writer_src = (_SRC / "billing" / "attribution_writer.py").read_text()
    accepted_line = next(
        line for line in writer_src.splitlines() if "cle.entry_type IN" in line
    )
    accepted = set(ast.literal_eval(accepted_line.split("IN", 1)[1].strip().rstrip(",")))
    produced = _producer_entry_types()
    assert produced, "the producer scan found nothing — it has stopped measuring anything"
    missing = produced - accepted
    assert not missing, (
        f"producers write {sorted(missing)} and the attribution reader does not accept it. This is "
        "the VT-754 defect exactly: the reader silently matches nothing and revenue attribution "
        "reads zero while looking implemented."
    )


def test_the_reader_does_not_accept_values_NOBODY_writes():
    """The other direction, softer: an accepted value with no producer is dead vocabulary that makes
    the predicate look broader than it is. 'payment' is grandfathered — historical rows carry it —
    so it is named explicitly rather than silently tolerated."""
    writer_src = (_SRC / "billing" / "attribution_writer.py").read_text()
    accepted_line = next(line for line in writer_src.splitlines() if "cle.entry_type IN" in line)
    accepted = set(ast.literal_eval(accepted_line.split("IN", 1)[1].strip().rstrip(",")))
    orphans = accepted - _producer_entry_types() - {"payment"}
    assert not orphans, f"the reader accepts {sorted(orphans)}, which no producer writes"


# --- scope 1: an attributable signal is REQUIRED -------------------------------------------------


def _tenant(conn) -> str:
    return str(
        conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('vt754 attribution', 'founding', 'trial') RETURNING id"
        ).fetchone()["id"]
    )


@pytest.mark.integration
def test_a_coincident_sale_with_NO_signal_earns_NOTHING(_dbpool):
    """The ruling in one test. Before D-C this exact shape produced an attribution row and an
    owner-facing revenue number; the shop simply made a sale that week."""
    import psycopg

    from orchestrator.billing.attribution_writer import build_campaign_attributions

    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)
        cust, camp = _seed_recipient_and_campaign(conn, tid)
        _seed_sale(conn, tid, cust, 50000)
        with conn.cursor() as cur:
            n = build_campaign_attributions(cur, tid, camp, _close_at())
    assert n == 0, "a sale with no click and no reply was credited to the campaign"


@pytest.mark.integration
def test_a_click_then_a_sale_IS_attributed(_dbpool):
    import psycopg

    from orchestrator.billing.attribution_writer import build_campaign_attributions

    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)
        cust, camp = _seed_recipient_and_campaign(conn, tid)
        _seed_click(conn, tid, cust, days_ago=3)
        _seed_sale(conn, tid, cust, 50000)
        with conn.cursor() as cur:
            n = build_campaign_attributions(cur, tid, camp, _close_at())
        row = conn.execute(
            "SELECT attribution_method, attributed_paise FROM attributions WHERE campaign_id = %s",
            (camp,),
        ).fetchone()
    assert n == 1
    assert row["attribution_method"] == "tracked_link", (
        "the method must NAME the signal — 'window_match' is the over-claiming inference D-C "
        "rejects, and a reader cannot tell an honest row from it"
    )
    assert row["attributed_paise"] == 50000


@pytest.mark.integration
def test_a_sale_BEFORE_the_click_is_not_attributed(_dbpool):
    """Direction is the whole claim: a purchase that precedes its "signal" is a coincidence with
    better timing."""
    import psycopg

    from orchestrator.billing.attribution_writer import build_campaign_attributions

    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)
        cust, camp = _seed_recipient_and_campaign(conn, tid)
        _seed_click(conn, tid, cust, days_ago=1)
        _seed_sale(conn, tid, cust, 50000, days_ago=4)
        with conn.cursor() as cur:
            n = build_campaign_attributions(cur, tid, camp, _close_at())
    assert n == 0


@pytest.mark.integration
def test_a_reply_then_a_sale_IS_attributed_when_the_caller_supplies_the_token(_dbpool):
    """The reply half. The token cannot be derived inside the writer (salted hash, and `customers` is
    VT-72-watched), so the caller supplies it — and if it does not, this half simply finds nothing,
    which errs UNDER."""
    import psycopg

    from orchestrator.billing.attribution_writer import build_campaign_attributions

    dsn = os.environ["DATABASE_URL"]
    token = f"phone_tok_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)
        cust, camp = _seed_recipient_and_campaign(conn, tid)
        from datetime import timedelta

        conn.execute(
            "INSERT INTO wa_conversations (tenant_id, phone_token, last_inbound_at) "
            "VALUES (%s, %s, %s)",
            (tid, token, _close_at() - timedelta(days=3)),
        )
        _seed_sale(conn, tid, cust, 30000)
        with conn.cursor() as cur:
            without = build_campaign_attributions(cur, tid, camp, _close_at())
            with_token = build_campaign_attributions(
                cur, tid, camp, _close_at(), reply_tokens=[token]
            )
        row = conn.execute(
            "SELECT attribution_method FROM attributions WHERE campaign_id = %s", (camp,)
        ).fetchone()
    assert without == 0, "the reply half fired without the caller supplying a token"
    assert with_token == 1
    assert row["attribution_method"] == "reply_then_purchase"


# --- helpers -------------------------------------------------------------------------------------


def _close_at():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _seed_recipient_and_campaign(conn, tid: str) -> tuple[str, str]:
    cust = str(
        conn.execute(
            "INSERT INTO customers (tenant_id, display_name, source) "
            "VALUES (%s, 'vt754 cust', 'test') RETURNING id",
            (tid,),
        ).fetchone()["id"]
    )
    run_id = str(
        conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, status, started_at) "
            "VALUES (%s, 'completed', now() - interval '8 days') RETURNING id",
            (tid,),
        ).fetchone()["id"]
    )
    camp = str(
        conn.execute(
            "INSERT INTO campaigns (tenant_id, run_id, status, generated_at, plan_json) "
            "VALUES (%s, %s, 'sent', now(), '{}'::jsonb) RETURNING id",
            (tid, run_id),
        ).fetchone()["id"]
    )
    conn.execute(
        "INSERT INTO campaign_recipients (campaign_id, customer_id, tenant_id) VALUES (%s, %s, %s)",
        (camp, cust, tid),
    )
    return cust, camp


def _seed_sale(conn, tid: str, cust: str, paise: int, *, days_ago: int = 0) -> None:
    """Dates are computed in PYTHON from the same UTC clock `_close_at()` uses — never DB `now()`.
    The writer's window is `close_at.date()` in UTC; a DB session in IST puts `now()::date` one day
    ahead between 00:00 and 05:30 IST, and the pre-push hook found exactly that (a sale dated
    "tomorrow" fell outside the window at 02:46 IST). A test that passes only in daylight is worse
    than one that fails honestly."""
    from datetime import timedelta

    day = (_close_at() - timedelta(days=days_ago)).date()
    conn.execute(
        "INSERT INTO customer_ledger_entries "
        "(tenant_id, customer_id, amount_paise, entry_type, entry_date, acquired_via, "
        " source_confidence, entry_key) "
        "VALUES (%s, %s, %s, 'sale', %s, 'upi_gpay', 0.9, %s)",
        (tid, cust, paise, day, str(uuid.uuid4())),
    )


def _seed_click(conn, tid: str, cust: str, *, days_ago: int) -> None:
    from datetime import timedelta

    at = _close_at() - timedelta(days=days_ago)
    token = f"tok-{uuid.uuid4().hex[:16]}"
    conn.execute(
        "INSERT INTO hook_links (token, tenant_id, source) VALUES (%s, %s, 'test')", (token, tid)
    )
    conn.execute(
        "INSERT INTO customer_hook_links "
        "(tenant_id, customer_id, token, source, click_count, first_clicked_at, last_clicked_at) "
        "VALUES (%s, %s, %s, 'test', 1, %s, %s)",
        (tid, cust, token, at, at),
    )


# --- scope 4: the sweep can only run if something arms the window --------------------------------


@pytest.mark.integration
def test_marking_a_campaign_SENT_arms_the_attribution_window(_dbpool):
    """Nothing in `src/` ever set `campaigns.attribution_close_at` — 1 of 34 rows on dev had it — and
    the sweep scans `WHERE attribution_close_at IS NOT NULL AND <= now()`. So a perfectly correct
    join would still never have run on schedule, and the number would have stayed zero for a SECOND,
    independent reason that looks identical to the first.
    """
    import psycopg

    from orchestrator.billing.attribution_writer import ATTRIBUTION_WINDOW_DAYS
    from orchestrator.db.wrappers import CampaignsWrapper

    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)
        _cust, camp = _seed_recipient_and_campaign(conn, tid)
        conn.execute("UPDATE campaigns SET status='proposed', attribution_close_at=NULL WHERE id=%s", (camp,))

        CampaignsWrapper().set_status(tid, camp, "sent")
        row = conn.execute(
            "SELECT attribution_close_at, "
            "       EXTRACT(EPOCH FROM (attribution_close_at - now()))/86400 AS days_out "
            "  FROM campaigns WHERE id = %s",
            (camp,),
        ).fetchone()
    assert row["attribution_close_at"] is not None, "the send did not arm the attribution window"
    assert abs(float(row["days_out"]) - ATTRIBUTION_WINDOW_DAYS) < 0.01, (
        "the window is not send_at + ATTRIBUTION_WINDOW_DAYS — a second seven has entered the system"
    )


@pytest.mark.integration
def test_a_re_send_does_not_MOVE_a_window_that_is_already_running(_dbpool):
    """Re-arming on every status write would let a correction or a redelivery slide the close date
    forward indefinitely, so a campaign's attribution would never close."""
    import psycopg

    from orchestrator.db.wrappers import CampaignsWrapper

    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)
        _cust, camp = _seed_recipient_and_campaign(conn, tid)
        CampaignsWrapper().set_status(tid, camp, "sent")
        first = conn.execute(
            "SELECT attribution_close_at FROM campaigns WHERE id = %s", (camp,)
        ).fetchone()["attribution_close_at"]
        CampaignsWrapper().set_status(tid, camp, "sent")
        second = conn.execute(
            "SELECT attribution_close_at FROM campaigns WHERE id = %s", (camp,)
        ).fetchone()["attribution_close_at"]
    assert first == second, "a second 'sent' write moved the attribution window forward"


@pytest.mark.integration
def test_a_NON_sent_status_write_does_not_arm_anything(_dbpool):
    import psycopg

    from orchestrator.db.wrappers import CampaignsWrapper

    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)
        _cust, camp = _seed_recipient_and_campaign(conn, tid)
        conn.execute("UPDATE campaigns SET attribution_close_at=NULL WHERE id=%s", (camp,))
        CampaignsWrapper().set_status(tid, camp, "cancelled")
        row = conn.execute(
            "SELECT attribution_close_at FROM campaigns WHERE id = %s", (camp,)
        ).fetchone()
    assert row["attribution_close_at"] is None
