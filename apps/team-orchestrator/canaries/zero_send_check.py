"""VT-611 — zero-real-send verification (team-lead's hard-stop guard, 2026-07-06).

Fazal's invariant: no real customer WhatsApp send before sign-off. The dev_send_guard mocks every
outbound send with a synthetic ``MKDEV{27 hex}`` SID (src/orchestrator/utils/dev_send_guard.py) —
a real Twilio send SID never carries that prefix. This tool counts any OUTBOUND send SID captured
during the run's time window that is NOT ``MKDEV``-prefixed, across every table an outbound send
can land in:

  - ``send_idempotency_keys.message_sid``  (VT-44 outbound ledger)
  - ``campaign_messages.message_sid``      (VT-44/45 per-recipient campaign sends)
  - ``conversation_log.message_sid`` WHERE ``role = 'assistant'`` (VT-579 — owner-role rows are
    harness-INBOUND-injected ``SMharness...`` sids, not outbound sends, and are deliberately
    excluded; only assistant-authored/outbound rows are a "send" for this check)

A non-zero count is a HARD STOP (team-lead, 2026-07-06) — not a caveat, not something the manifest
merely notes. This tool exits non-zero and prints every offending SID's table + tenant so the run
can be halted and investigated immediately.

Run via a SERVICE-ROLE / privileged connection (same posture as shadow_gate_check.py — CL-431
by-reference, this script never prints the DSN):

    railway run --service vt-orchestrator-service --environment development -- \\
        uv run --directory apps/team-orchestrator python canaries/zero_send_check.py \\
        --since 2026-07-06T18:00:00Z [--json out.json]

Exits 0 only when the count is exactly zero.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_MOCK_PREFIX = "MKDEV"


@dataclass(frozen=True)
class RealSendFinding:
    table: str
    tenant_id: str
    message_sid: str


@dataclass(frozen=True)
class RealSendReport:
    since: str
    total_checked: int
    real_send_count: int
    findings: list[RealSendFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.real_send_count == 0


def _is_real_send(sid: str | None) -> bool:
    """A send SID counts as a REAL (non-mocked) send iff it's present and NOT the dev_send_guard's
    mock prefix. NULL sids (a send that failed before a SID was assigned) are not a "real send"."""
    return bool(sid) and not str(sid).startswith(_MOCK_PREFIX)


def classify_rows(table: str, rows: list[tuple[str, str | None]]) -> list[RealSendFinding]:
    """Pure — ``rows`` is (tenant_id, message_sid) pairs already fetched from one table. Isolated
    from the DB round-trip so this classification is unit-testable without a live connection."""
    return [
        RealSendFinding(table=table, tenant_id=tenant_id, message_sid=str(sid))
        for tenant_id, sid in rows
        if _is_real_send(sid)
    ]


def _fetch_rows(conn: Any, table: str, sid_column: str, since: datetime, *, extra_where: str = "") -> list[tuple[str, str | None]]:
    query = f"SELECT tenant_id, {sid_column} FROM {table} WHERE created_at >= %s{extra_where}"
    rows = conn.execute(query, (since,)).fetchall()
    out: list[tuple[str, str | None]] = []
    for r in rows:
        if isinstance(r, dict):
            out.append((str(r["tenant_id"]), r[sid_column]))
        else:
            out.append((str(r[0]), r[1]))
    return out


def capture_zero_send_report(conn: Any, since: datetime) -> RealSendReport:
    """The S-equivalent evidence query for the zero-real-send guard: scans every table an outbound
    send can land a SID in, classifies each against the dev_send_guard mock prefix, and reports
    ANY real (non-mocked) send SID found since the run's own start time."""
    findings: list[RealSendFinding] = []
    total = 0

    idem_rows = _fetch_rows(conn, "send_idempotency_keys", "message_sid", since)
    total += len(idem_rows)
    findings += classify_rows("send_idempotency_keys", idem_rows)

    campaign_rows = _fetch_rows(conn, "campaign_messages", "message_sid", since)
    total += len(campaign_rows)
    findings += classify_rows("campaign_messages", campaign_rows)

    convo_rows = _fetch_rows(
        conn, "conversation_log", "message_sid", since, extra_where=" AND role = 'assistant'",
    )
    total += len(convo_rows)
    findings += classify_rows("conversation_log", convo_rows)

    return RealSendReport(
        since=since.isoformat(), total_checked=total, real_send_count=len(findings), findings=findings,
    )


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not dsn:
        print(
            "zero_send_check: ERROR: no DB URL in env (DATABASE_URL / TEAM_SUPABASE_DB_URL) — "
            "run under `railway run --environment development`",
            file=sys.stderr,
        )
        sys.exit(2)
    return dsn


def _parse_since(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zero_send_check", description=__doc__)
    p.add_argument("--since", required=True, help="ISO-8601 timestamp the run batch started")
    p.add_argument("--json", default=None, help="write the report — for the evidence manifest's --real-send-count")
    return p


def main(argv: list[str] | None = None) -> int:
    import psycopg

    args = build_parser().parse_args(argv)
    since = _parse_since(args.since)
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        report = capture_zero_send_report(conn, since)

    print(f"zero_send_check since={args.since}: total_checked={report.total_checked} "
          f"real_send_count={report.real_send_count}")
    for f in report.findings:
        print(f"  REAL SEND DETECTED — table={f.table} tenant={f.tenant_id} sid={f.message_sid}")
    if report.passed:
        print("zero_send_check: PASS — every observed send was dev_send_guard-mocked (MKDEV).")
    else:
        print("zero_send_check: HARD STOP — a real (non-mocked) send SID was found. "
              "Per team-lead's 2026-07-06 guard: STOP the run immediately and investigate.")

    if args.json:
        payload = {
            "since": report.since, "total_checked": report.total_checked,
            "real_send_count": report.real_send_count,
            "findings": [
                {"table": f.table, "tenant_id": f.tenant_id, "message_sid": f.message_sid}
                for f in report.findings
            ],
            "passed": report.passed,
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"    json: wrote {args.json} — for vt611_evidence_manifest.py's --real-send-count")

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
