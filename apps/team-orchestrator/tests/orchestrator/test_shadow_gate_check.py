"""VT-611 Package S — shadow_gate_check.py.

Pure tests for check_preflight/check_gate (no DB), plus a realdb test for capture_shadow_evidence
against a real tm_audit_log table (mig147).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import shadow_gate_check as sgc  # noqa: E402


# --- pure: check_preflight / check_gate ------------------------------------------------------


def test_check_preflight_fails_on_zero_evals():
    ev = sgc.ShadowEvidence(total_evals=0, distinct_conversations=0, safety_divergences=0)
    failures = sgc.check_preflight(ev)
    assert failures and "DO NOT launch" in failures[0]


def test_check_preflight_passes_on_at_least_one_eval():
    ev = sgc.ShadowEvidence(total_evals=1, distinct_conversations=1, safety_divergences=0)
    assert sgc.check_preflight(ev) == []


def test_check_gate_passes_at_exactly_min_distinct_and_zero_divergences():
    ev = sgc.ShadowEvidence(total_evals=60, distinct_conversations=50, safety_divergences=0)
    assert sgc.check_gate(ev, min_distinct=50) == []


def test_check_gate_fails_below_min_distinct():
    ev = sgc.ShadowEvidence(total_evals=40, distinct_conversations=49, safety_divergences=0)
    failures = sgc.check_gate(ev, min_distinct=50)
    assert any("distinct_conversations" in f for f in failures)


def test_check_gate_fails_on_any_safety_divergence_even_with_enough_conversations():
    """The hard-zero requirement: plenty of conversations does NOT offset even 1 divergence."""
    ev = sgc.ShadowEvidence(total_evals=200, distinct_conversations=100, safety_divergences=1)
    failures = sgc.check_gate(ev, min_distinct=50)
    assert any("safety_divergences" in f for f in failures)
    assert not any("distinct_conversations" in f for f in failures)


def test_check_gate_reports_both_failures_when_both_hold():
    ev = sgc.ShadowEvidence(total_evals=5, distinct_conversations=3, safety_divergences=2)
    failures = sgc.check_gate(ev, min_distinct=50)
    assert len(failures) == 2


def test_parse_since_accepts_bare_z_suffix():
    dt = sgc._parse_since("2026-07-07T00:00:00Z")
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.month == 7 and dt.day == 7


# --- realdb: capture_shadow_evidence (skipped without DATABASE_URL via the fixture below) -------


@pytest.fixture(scope="module")
def dsn():
    # Skip (not error) when psycopg/DATABASE_URL are absent — the dep-less CI smoke job (no
    # psycopg) and a plain local run (no DATABASE_URL) both hit this fixture; a bare
    # `import apply_migrations` / `os.environ["DATABASE_URL"]` blew up as an ERROR in both cases,
    # which fails the pre-push dep-less smoke stage. The PURE tests above (check_preflight/
    # check_gate/_parse_since) need neither and must keep running regardless — so the guard lives
    # here, not as a module-level pytestmark/importorskip that would also skip those.
    pytest.importorskip("psycopg")
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — shadow_gate_check realdb tests skipped")
    import apply_migrations
    r = apply_migrations.apply(dsn=url)
    assert not r["failed"], r["failed"]
    return url


def _new_tenant(dsn: str) -> str:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at) "
            "VALUES ('convo-harness-shadow-test', 'founding', 'trial', now()) RETURNING id"
        ).fetchone()
    return str(row[0])


def _insert_audit_row(
    dsn: str, tenant_id: str, *, event_kind: str, status: str, created_at: datetime | None = None
) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tm_audit_log (tenant_id, event_layer, event_kind, actor, status, created_at) "
            "VALUES (%s, 'decides', %s, 'team_manager', %s, COALESCE(%s, now()))",
            (tenant_id, event_kind, status, created_at),
        )


def _now_cutoff(dsn: str) -> datetime:
    """The DB's OWN clock, right now — used as ``since`` so a test's window starts strictly AFTER
    any earlier test's rows (this fixture is module-scoped; tests share one table) and matches
    what the DB itself will compare against (avoids any app-server/DB clock skew)."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute("SELECT now()").fetchone()
    return row[0] if not isinstance(row, dict) else row["now"]


def test_capture_shadow_evidence_counts_distinct_tenants_and_blocked(dsn):
    since = _now_cutoff(dsn)
    t1, t2 = _new_tenant(dsn), _new_tenant(dsn)
    _insert_audit_row(dsn, t1, event_kind="shadow_divergence", status="ok")
    _insert_audit_row(dsn, t1, event_kind="shadow_divergence", status="ok")  # same tenant, 2nd turn
    _insert_audit_row(dsn, t2, event_kind="shadow_divergence", status="blocked")

    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        evidence = sgc.capture_shadow_evidence(conn, since)

    assert evidence.total_evals == 3
    assert evidence.distinct_conversations == 2  # COUNT(DISTINCT tenant_id), not raw rows (B6)
    assert evidence.safety_divergences == 1


def test_capture_shadow_evidence_ignores_other_event_kinds(dsn):
    since = _now_cutoff(dsn)
    tenant = _new_tenant(dsn)
    _insert_audit_row(dsn, tenant, event_kind="spawn", status="ok")
    _insert_audit_row(dsn, tenant, event_kind="draft_created", status="blocked")

    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        evidence = sgc.capture_shadow_evidence(conn, since)

    assert evidence.total_evals == 0
    assert evidence.safety_divergences == 0


def test_capture_shadow_evidence_respects_since_timestamp(dsn):
    """A row from BEFORE the flip must not count (avoids a stale prior shadow-mode window
    contaminating this run's evidence)."""
    tenant = _new_tenant(dsn)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    _insert_audit_row(dsn, tenant, event_kind="shadow_divergence", status="ok", created_at=old)

    since = _now_cutoff(dsn)
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        evidence = sgc.capture_shadow_evidence(conn, since)
    assert evidence.total_evals == 0


def test_capture_shadow_evidence_none_row_is_all_zero(dsn):
    """Defensive: if the query somehow returns no row at all (never happens with a real count(*),
    but a stub/mock double might), treat it as all-zero rather than raising."""
    class _FakeConn:
        def execute(self, *a, **k):
            return self

        def fetchone(self):
            return None

    evidence = sgc.capture_shadow_evidence(_FakeConn(), datetime.now(timezone.utc))
    assert evidence == sgc.ShadowEvidence(total_evals=0, distinct_conversations=0, safety_divergences=0)


def test_main_preflight_exit_codes(dsn, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", dsn)
    since_str = _now_cutoff(dsn).isoformat()

    # zero rows since the cutoff -> exit 1
    rc = sgc.main(["preflight", "--since", since_str])
    assert rc == 1

    tenant = _new_tenant(dsn)
    _insert_audit_row(dsn, tenant, event_kind="shadow_divergence", status="ok")
    rc = sgc.main(["preflight", "--since", since_str])
    assert rc == 0


def test_main_evidence_exit_codes(dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", dsn)
    since_str = _now_cutoff(dsn).isoformat()

    rc = sgc.main(["evidence", "--since", since_str, "--min-distinct", "2"])
    assert rc == 1  # zero distinct conversations since the cutoff


def test_main_evidence_writes_json_for_the_manifest(dsn, monkeypatch, tmp_path):
    """The evidence manifest can't quote a print statement — --json must persist the SAME numbers
    the gate check is scoring, verbatim, so the manifest cites the evidence rather than re-deriving
    it (or worse, hand-transcribing it from a terminal scrollback)."""
    monkeypatch.setenv("DATABASE_URL", dsn)
    since = _now_cutoff(dsn)
    since_str = since.isoformat()
    tenant_a = _new_tenant(dsn)
    tenant_b = _new_tenant(dsn)
    _insert_audit_row(dsn, tenant_a, event_kind="shadow_divergence", status="ok")
    _insert_audit_row(dsn, tenant_b, event_kind="shadow_divergence", status="ok")

    out_path = tmp_path / "shadow_evidence.json"
    rc = sgc.main([
        "evidence", "--since", since_str, "--min-distinct", "2", "--json", str(out_path),
    ])
    assert rc == 0
    payload = json.loads(out_path.read_text())
    assert payload["distinct_conversations"] == 2
    assert payload["safety_divergences"] == 0
    assert payload["passed"] is True
    assert payload["failures"] == []
    assert payload["since"] == since_str


def test_main_evidence_json_records_failure_when_gate_fails(dsn, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", dsn)
    since_str = _now_cutoff(dsn).isoformat()

    out_path = tmp_path / "shadow_evidence.json"
    rc = sgc.main([
        "evidence", "--since", since_str, "--min-distinct", "50", "--json", str(out_path),
    ])
    assert rc == 1
    payload = json.loads(out_path.read_text())
    assert payload["passed"] is False
    assert payload["failures"]

    t1, t2 = _new_tenant(dsn), _new_tenant(dsn)
    _insert_audit_row(dsn, t1, event_kind="shadow_divergence", status="ok")
    _insert_audit_row(dsn, t2, event_kind="shadow_divergence", status="ok")
    rc = sgc.main(["evidence", "--since", since_str, "--min-distinct", "2"])
    assert rc == 0
