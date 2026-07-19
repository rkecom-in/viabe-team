"""VT-611 — zero_send_check.py (team-lead's hard-stop guard, 2026-07-06).

Pure tests for _is_real_send/classify_rows (no DB), plus a realdb test for
capture_zero_send_report against real send_idempotency_keys/campaign_messages/conversation_log
tables.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import zero_send_check as zsc  # noqa: E402

pytest.importorskip("psycopg")


# --- pure: _is_real_send / classify_rows -----------------------------------------------------------


def test_is_real_send_false_for_mkdev_prefix():
    assert zsc._is_real_send("MKDEV" + "a" * 27) is False


def test_is_real_send_false_for_null():
    assert zsc._is_real_send(None) is False


def test_is_real_send_true_for_a_real_looking_twilio_sid():
    assert zsc._is_real_send("SM" + "0" * 32) is True


def test_classify_rows_finds_only_the_non_mkdev_sids():
    rows = [
        ("t1", "MKDEV" + "a" * 27),
        ("t2", "SMrealsid00000000000000000000000"),
        ("t3", None),
        ("t4", "MKDEV" + "b" * 27),
    ]
    findings = zsc.classify_rows("send_idempotency_keys", rows)
    assert len(findings) == 1
    assert findings[0].tenant_id == "t2"
    assert findings[0].table == "send_idempotency_keys"


def test_classify_rows_empty_when_all_mocked():
    rows = [("t1", "MKDEV" + "x" * 27), ("t2", None)]
    assert zsc.classify_rows("campaign_messages", rows) == []


# --- RealSendReport.passed --------------------------------------------------------------------------


def test_report_passed_true_when_zero_real_sends():
    report = zsc.RealSendReport(since="x", total_checked=10, real_send_count=0, findings=[])
    assert report.passed is True


def test_report_passed_false_when_any_real_send():
    finding = zsc.RealSendFinding(table="t", tenant_id="x", message_sid="SMreal")
    report = zsc.RealSendReport(since="x", total_checked=1, real_send_count=1, findings=[finding])
    assert report.passed is False


# --- realdb: capture_zero_send_report (skipped without DATABASE_URL) -------------------------------


@pytest.fixture(scope="module")
def dsn():
    pytest.importorskip("psycopg")
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — zero_send_check realdb tests skipped")
    import apply_migrations

    r = apply_migrations.apply(dsn=url)
    assert not r["failed"], r["failed"]
    return url


def _now_cutoff(dsn: str) -> datetime:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute("SELECT now()").fetchone()
    return row[0] if not isinstance(row, dict) else row["now"]


def _new_tenant(dsn: str) -> str:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at) "
            "VALUES ('zero-send-check-test', 'founding', 'trial', now()) RETURNING id"
        ).fetchone()
    return str(row[0] if not isinstance(row, dict) else row["id"])


def _insert_idem_row(dsn: str, tenant_id: str, message_sid: str | None) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO send_idempotency_keys (tenant_id, idempotency_key, message_sid, send_status) "
            "VALUES (%s, %s, %s, 'sent')",
            (tenant_id, str(uuid4()), message_sid),
        )


def _insert_convo_row(dsn: str, tenant_id: str, role: str, message_sid: str | None) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO conversation_log (tenant_id, role, surface, text, message_sid) "
            "VALUES (%s, %s, 'manager', 'hi', %s)",
            (tenant_id, role, message_sid),
        )


def test_capture_zero_send_report_passes_when_only_mkdev_sids(dsn):
    since = _now_cutoff(dsn)
    tenant = _new_tenant(dsn)
    _insert_idem_row(dsn, tenant, "MKDEV" + "a" * 27)
    _insert_convo_row(dsn, tenant, "assistant", "MKDEV" + "b" * 27)

    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        report = zsc.capture_zero_send_report(conn, since)
    assert report.passed is True
    assert report.real_send_count == 0


def test_capture_zero_send_report_fails_on_a_real_sid_in_idem_keys(dsn):
    since = _now_cutoff(dsn)
    tenant = _new_tenant(dsn)
    _insert_idem_row(dsn, tenant, "SMrealsid00000000000000000000000")

    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        report = zsc.capture_zero_send_report(conn, since)
    assert report.passed is False
    assert any(f.table == "send_idempotency_keys" for f in report.findings)


def test_capture_zero_send_report_ignores_owner_role_convo_rows(dsn):
    """An owner-role conversation_log row is a harness-INBOUND-injected SID (SMharness...), not an
    outbound send — must never be flagged even though it isn't MKDEV-prefixed."""
    since = _now_cutoff(dsn)
    tenant = _new_tenant(dsn)
    _insert_convo_row(dsn, tenant, "owner", "SMharness00000000000000000000000")

    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        report = zsc.capture_zero_send_report(conn, since)
    assert report.passed is True
    assert report.real_send_count == 0


def test_capture_zero_send_report_respects_since_timestamp(dsn):
    tenant = _new_tenant(dsn)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO send_idempotency_keys (tenant_id, idempotency_key, message_sid, "
            "send_status, created_at) VALUES (%s, %s, %s, 'sent', %s)",
            (tenant, str(uuid4()), "SMrealsid00000000000000000000000", old),
        )
    since = _now_cutoff(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        report = zsc.capture_zero_send_report(conn, since)
    assert report.total_checked == 0
