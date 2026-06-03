"""VT-153 — pre-#45 plaintext-body backfill canary.

mig 090 scrubs the VT-144 redaction family ({body, message_body, raw_text,
content}) from EXISTING pipeline_runs.trigger_payload + pipeline_steps
input/output_envelope. VT-144 only stopped FORWARD persistence; this scrubs the
historical population. CL-422 synthetic (no real data on dev).

Asserts (DR-15): seed body-bearing rows → run mig 090 → ZERO body-family keys in
either table · a clean row is untouched · the GENERATED FTS tsvector no longer
matches the body text · a 2nd run touches 0 rows (idempotent via the `?|` guard).
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-153 backfill canary skipped",
)

import psycopg  # noqa: E402 — after the skip guard

_MIG = (
    Path(__file__).resolve().parents[4] / "migrations" / "090_vt153_backfill_body_scrub.sql"
)
_BODY_KEYS = ("body", "message_body", "raw_text", "content")


def _statements(sql: str) -> list[str]:
    """Strip `--` comment lines + blank lines, split into statements on `;`."""
    no_comments = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    return [s.strip() for s in no_comments.split(";") if s.strip()]


def _run_migration(conn) -> int:  # type: ignore[no-untyped-def]
    """Execute mig 090's UPDATEs; return total rows affected across them."""
    total = 0
    for stmt in _statements(_MIG.read_text()):
        cur = conn.execute(stmt)
        total += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return total


def _tenant(conn) -> UUID:  # type: ignore[no-untyped-def]
    row = conn.execute(
        "INSERT INTO tenants (business_name, plan_tier, phase) "
        "VALUES ('vt153', 'founding', 'paid_active') RETURNING id"
    ).fetchone()
    return UUID(str(row[0]))


def test_vt153_backfill_scrubs_body_idempotently():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]

    with psycopg.connect(dsn, autocommit=True) as conn:
        tid = _tenant(conn)

        # Dirty pipeline_runs row: body-family keys + a legit key.
        dirty_run = uuid4()
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status, trigger_payload) "
            "VALUES (%s, %s, 'twilio_inbound', 'completed', "
            "  %s::jsonb)",
            (
                str(dirty_run), str(tid),
                '{"body": "secret customer message", "message_body": "x", '
                '"raw_text": "y", "content": "z", "MessageSid": "SM123"}',
            ),
        )
        # Clean pipeline_runs row: no body family — must stay byte-identical.
        clean_run = uuid4()
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status, trigger_payload) "
            "VALUES (%s, %s, 'twilio_inbound', 'completed', %s::jsonb)",
            (str(clean_run), str(tid), '{"MessageSid": "SM999", "from": "+9100"}'),
        )

        # Dirty pipeline_steps row: body in input_envelope, content in output_envelope.
        conn.execute(
            "INSERT INTO pipeline_steps "
            "(run_id, tenant_id, step_seq, step_kind, status, input_envelope, output_envelope) "
            "VALUES (%s, %s, 0, 'webhook_received', 'completed', %s::jsonb, %s::jsonb)",
            (
                str(dirty_run), str(tid),
                '{"body": "secret message text", "kind": "inbound"}',
                '{"content": "echoed secret", "result": "ok"}',
            ),
        )

        # --- run the migration ------------------------------------------------
        affected = _run_migration(conn)
        assert affected >= 3, f"expected the 3 dirty rows scrubbed, got {affected}"

        # 1. ZERO body-family keys remain anywhere in the seeded rows.
        run_payload = conn.execute(
            "SELECT trigger_payload FROM pipeline_runs WHERE id = %s", (str(dirty_run),)
        ).fetchone()[0]
        for key in _BODY_KEYS:
            assert key not in run_payload, f"{key} survived in trigger_payload"
        assert run_payload.get("MessageSid") == "SM123", "non-body provenance must survive"

        step = conn.execute(
            "SELECT input_envelope, output_envelope FROM pipeline_steps WHERE run_id = %s",
            (str(dirty_run),),
        ).fetchone()
        for env in step:
            for key in _BODY_KEYS:
                assert key not in env, f"{key} survived in a pipeline_steps envelope"
        assert step[0].get("kind") == "inbound" and step[1].get("result") == "ok"

        # 2. The clean row is byte-identical (no spurious rewrite).
        clean = conn.execute(
            "SELECT trigger_payload FROM pipeline_runs WHERE id = %s", (str(clean_run),)
        ).fetchone()[0]
        assert clean == {"MessageSid": "SM999", "from": "+9100"}

        # 3. The GENERATED FTS tsvector (mig 038) no longer matches the body text —
        #    stripping the JSONB auto-recomputed the stored tsvector.
        fts_hit = conn.execute(
            "SELECT count(*) FROM pipeline_steps "
            "WHERE run_id = %s AND envelope_search_tsv @@ plainto_tsquery('english', 'secret')",
            (str(dirty_run),),
        ).fetchone()[0]
        assert fts_hit == 0, "body tokens still searchable in the FTS index after scrub"

        # 4. Idempotent: a 2nd run touches 0 rows (the `?|` guard).
        assert _run_migration(conn) == 0, "migration is not idempotent — re-run hit rows"
