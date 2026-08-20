"""VT-752 item 1 — where the ~390s actually goes, measured at the boundaries that emit nothing.

WHAT WAS MEASURED, AND WHAT COULD NOT BE. On deployed dev, a campaign plan is emitted at **T+52s**
and the `campaigns` row lands at **T+480s**. The SR agent's own model call inside that window is
11-18s. So ~185s of the gap is spent BEFORE the specialist starts — in triage, task minting, the
durable-workflow handoff and the queue — and none of those transitions writes anything at all.

WHY THIS IS NOT `pipeline_steps`. Three things rule it out, each checked rather than assumed:

* `pipeline_steps` rows are point-in-time EVENT records — both writers set only `started_at`. Last
  24h on dev: 129 rows, **0** with `ended_at`, **0** with a non-zero `duration_ms`. All time: the
  only 100 rows that ever carried a duration are `mcp_tool_call`, newest 2026-05-27. The columns
  exist and the steps that matter have never had a writer.
* Every row needs a `run_id` FK. **The gap spans runs**: the owner's webhook run mints the task, and
  the durable `manager_task_workflow` that does the work has no run of its own. A per-run table
  structurally cannot measure a cross-run handoff — which is precisely the handoff that is slow.
* A new table would need a migration for pure instrumentation.

So the boundaries land on `tm_audit_log` via `emit_tm_audit` — already cross-run, already
tenant-scoped and redacted, already fail-soft, and keyed by a correlation id rather than a run.

THE CORRELATION KEY is the `manager_task` id where one exists, else the inbound `message_sid`. Both
are stable across the run boundary, which is the whole point.

EACH MARK CARRIES `total_ms` — elapsed since `manager_tasks.created_at`, i.e. since the owner's ask
became a durable task. Per-stage deltas are the consecutive differences, derived at read time.

WHY T0 IS THE TASK ROW AND NOT A PRIOR BOUNDARY. The obvious design — read this correlation's last
boundary and subtract — cannot work here, and the reason is worth recording because it is invisible
until you try it: **`tm_audit_log` is write-only for the app.** Migration 147 grants `app_role` an
INSERT policy and nothing else; the only SELECT policy requires an operator JWT claim. So a read-back
under `tenant_connection` returns zero rows, and every interval would have silently been `None` in
production while passing any test run with a superuser DSN. `manager_tasks.created_at` is durable,
already there, readable by `app_role`, and is the honest T0 anyway.

It also survives DBOS replay, which a Python-local start time would not: the workflow re-executes its
code path on recovery, so a timer captured in a local variable measures the replay, not the wait.

Fail-soft throughout: instrumentation that can break the path it measures is worse than no
instrumentation. Every failure degrades to "no numbers", never to an exception on the hot path.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

_EVENT_KIND = "stage_boundary"

__all__ = ["mark_stage", "read_stage_timeline"]


def mark_stage(
    tenant_id: UUID | str,
    stage: str,
    *,
    task_id: UUID | str | None = None,
    message_sid: str | None = None,
    run_id: UUID | str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Record one dispatch-stage boundary for ``(tenant, correlation)``.

    ``stage`` is a short, stable name (``task_minted``, ``workflow_start_requested``,
    ``workflow_picked_up``, ``specialist_dispatch``, …). ``total_ms`` is measured from
    ``manager_tasks.created_at`` on the SERVER clock, so callers thread no timing state through DBOS
    — which matters because the workflow side is replayed and a Python-local start time would
    measure the replay rather than the wait.

    Fail-soft: any error is logged and swallowed.
    """
    correlation = str(task_id) if task_id is not None else (message_sid or "")
    if not correlation:
        logger.warning("VT-752: mark_stage(%s) called with no correlation key — skipped", stage)
        return
    try:
        total_ms = _ms_since_task_created(tenant_id, task_id) if task_id is not None else None
        from orchestrator.observability.tm_audit import emit_tm_audit

        emit_tm_audit(
            event_layer="does",
            event_kind=_EVENT_KIND,
            actor="team_manager",
            tenant_id=tenant_id,
            run_id=run_id,
            summary=f"stage {stage}",
            result={
                "stage": stage,
                "correlation": correlation,
                "task_id": str(task_id) if task_id is not None else None,
                "message_sid": message_sid,
                "total_ms": total_ms,
                **(detail or {}),
            },
        )
    except Exception:  # noqa: BLE001 — instrumentation NEVER breaks the path it measures
        logger.warning("VT-752: stage mark %r failed (fail-soft) tenant=%s", stage, tenant_id)


def _ms_since_task_created(tenant_id: UUID | str, task_id: UUID | str) -> int | None:
    """Milliseconds from ``manager_tasks.created_at`` to now, measured entirely on the SERVER clock.

    One clock matters: the boundaries are written from different processes (the webhook worker and
    the DBOS workflow), and taking `now()` locally in each would fold clock skew straight into the
    numbers this row exists to trust.
    """
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT (EXTRACT(EPOCH FROM (now() - created_at)) * 1000)::bigint AS ms "
                "FROM manager_tasks WHERE tenant_id = %s AND id = %s",
                (str(tenant_id), str(task_id)),
            ).fetchone()
        if row is None:
            return None
        ms = row["ms"] if isinstance(row, dict) else row[0]
        return int(ms) if ms is not None else None
    except Exception:  # noqa: BLE001
        return None


@contextmanager
def _reader(tenant_id: UUID | str, dsn: str | None):
    """The analysis connection: an explicit privileged DSN when given, else the app connection."""
    if dsn:
        import psycopg

        with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
            yield conn
        return
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        yield conn


def read_stage_timeline(
    tenant_id: UUID | str, correlation: str, *, dsn: str | None = None
) -> list[dict[str, Any]]:
    """Every boundary for one correlation, oldest first, with per-stage ``elapsed_ms`` derived — the
    read side the gate (a) distribution is computed from.

    ANALYSIS ONLY, and it needs a privileged DSN. `tm_audit_log` has no `app_role` SELECT policy
    (migration 147), so this returns [] when called with ordinary app credentials — that is RLS
    working, not a bug. Run it with the operator/service connection the harness uses.

    Pass ``dsn`` to read through that privileged connection directly; omitted, it uses the app's
    ``tenant_connection`` and will legitimately see nothing.

    Returns [] rather than raising, so a reporting script can never take the pipeline down with it.
    """
    try:
        with _reader(tenant_id, dsn) as conn:
            rows = conn.execute(
                """
                SELECT created_at, result
                  FROM tm_audit_log
                 WHERE tenant_id = %(t)s AND event_kind = %(k)s
                   AND result ->> 'correlation' = %(c)s
                 ORDER BY created_at ASC
                """,
                {"t": str(tenant_id), "k": _EVENT_KIND, "c": correlation},
            ).fetchall()
        out: list[dict[str, Any]] = []
        prev = None
        for r in rows:
            created = r["created_at"] if isinstance(r, dict) else r[0]
            result = (r["result"] if isinstance(r, dict) else r[1]) or {}
            entry = {"at": created, **dict(result)}
            # Per-stage delta, derived here rather than stamped at write time — see the module note
            # on tm_audit_log being write-only for the app.
            entry["elapsed_ms"] = (
                None if prev is None else int((created - prev["at"]).total_seconds() * 1000)
            )
            entry["prev_stage"] = None if prev is None else prev.get("stage")
            out.append(entry)
            prev = entry
        return out
    except Exception:  # noqa: BLE001
        logger.warning("VT-752: stage timeline read failed tenant=%s", tenant_id)
        return []
