"""DBOS workflow_status purge — close the third Twilio-Body retention sink.

Step-0 (page 368387c2-cc5a-8128-a339-c2eb87a03f82, Branch B) confirmed that
the inbound Twilio ``Body`` field persists verbatim in
``dbos.workflow_status.inputs`` because the @DBOS.workflow webhook runner
receives ``twilio_fields`` (the full dict including ``Body``) as its
first-class argument, which DBOS serialises into the system DB so the
workflow can be re-invoked on recovery (``_core.py:execute_workflow_by_id``
deserialises ``status["inputs"]`` at recovery time).

Body is replay-critical — redacting it before ``start_workflow`` would
leave the recovered workflow re-invoked with ``event.body = ""``, which
diverges pre_filter routing between original and resumed runs. The
mitigation is a short-cadence purge of TERMINAL workflows: DBOS's
``garbage_collect`` filter excludes PENDING / ENQUEUED / DELAYED, so
in-flight and crashed-pending workflows (the rows recovery actually
needs) are never deleted by this sweep.

Retention SLA — ``WORKFLOW_INPUT_RETENTION_SECONDS`` (default 7200 / 2h).
Combined with the 30-min cadence the worst-case Body retention after a
successful inbound workflow is ~2.5h. The retention number is the
privacy knob; the cadence is a tuned constant (faster cadence does not
shrink Body lifetime by more than 30 min worst-case but does cost
sys-DB IO).

Public entry points:
  - ``purge_terminal_workflow_inputs()`` — invokable directly from tests
    and admin scripts. Returns ``(cutoff_ms, deleted_count)``.
  - ``purge_workflow_inputs_scheduled`` — the @DBOS.scheduled poller.
    Registered at module import; the registration is wired through
    ``dbos_config.launch_dbos`` so the decorator fires before
    ``DBOS.launch()`` and the poller thread starts with the rest.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

from dbos import DBOS
from dbos._workflow_commands import garbage_collect as _dbos_garbage_collect

logger = logging.getLogger(__name__)

# Privacy-facing knob — the longest the raw Twilio ``Body`` text persists
# in ``dbos.workflow_status.inputs`` after the inbound workflow reaches a
# terminal state. 2 hours by default; production / staging may tune via
# env var without code change.
_DEFAULT_RETENTION_SECONDS = 2 * 60 * 60
_RETENTION_ENV_VAR = "WORKFLOW_INPUT_RETENTION_SECONDS"

# Cadence — fixed constant, not env-configurable. Every 30 minutes. The
# retention SLA is the privacy knob; the cadence affects DB IO + worst-
# case lag, not the retention contract itself.
_PURGE_CRON = "*/30 * * * *"


def _retention_seconds() -> int:
    """Read the retention SLA from env, default 7200, fail-loud on bad input.

    Returns the configured seconds-of-retention. Invalid values
    (non-int / <=0) log a warning and fall back to the default — never
    silently apply an unsafe (e.g. negative) cutoff that would delete
    in-flight workflows past the status filter, and never raise into
    the scheduler thread.
    """
    raw = os.environ.get(_RETENTION_ENV_VAR)
    if raw is None:
        return _DEFAULT_RETENTION_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; falling back to default %ds",
            _RETENTION_ENV_VAR,
            raw,
            _DEFAULT_RETENTION_SECONDS,
        )
        return _DEFAULT_RETENTION_SECONDS
    if value <= 0:
        logger.warning(
            "%s=%d must be > 0; falling back to default %ds",
            _RETENTION_ENV_VAR,
            value,
            _DEFAULT_RETENTION_SECONDS,
        )
        return _DEFAULT_RETENTION_SECONDS
    return value


def _now_ms() -> int:
    """Current wall-clock in epoch milliseconds — matches the unit DBOS
    uses for ``workflow_status.created_at`` (BigInteger epoch_ms)."""
    return int(time.time() * 1000)


def purge_terminal_workflow_inputs() -> tuple[int, int]:
    """Run one GC sweep against ``dbos.workflow_status``.

    Computes ``cutoff_ms = now_ms - retention_s * 1000``, then delegates
    to ``dbos._workflow_commands.garbage_collect(dbos, cutoff_ms, None)``
    which:
      - Deletes ``workflow_status`` rows older than ``cutoff_ms`` whose
        ``status`` is NOT IN (PENDING, ENQUEUED, DELAYED) — i.e. only
        terminal-state rows (SUCCESS / ERROR / CANCELLED /
        MAX_RECOVERY_ATTEMPTS_EXCEEDED).
      - Cascades to ``operation_outputs`` / step ledger rows via
        ON DELETE CASCADE on the system schema.
      - Also purges ``app_db`` transaction outputs for the deleted
        workflow_ids (CL-71: tenant_id-scoped tables also free their
        rows).

    Returns ``(cutoff_ms, deleted_count)`` for the scheduler log + test
    assertions. ``deleted_count`` is an estimate (not perfectly
    accurate when rows_threshold is supplied; with our None call it is
    exact). Idempotent — concurrent invocations and zero-row sweeps
    both return cleanly without raising.
    """
    retention_s = _retention_seconds()
    cutoff_ms = _now_ms() - retention_s * 1000

    dbos_instance = DBOS._instance  # type: ignore[attr-defined]
    if dbos_instance is None:
        # DBOS.launch() has not run. The @DBOS.scheduled poller should
        # not fire before launch, but defend the direct-call surface
        # (tests / admin scripts) so an unconfigured environment does
        # not stack a stack trace into the log.
        logger.debug("workflow-input purge skipped: DBOS not launched")
        return cutoff_ms, 0

    # Pre-count rows that should be deleted, for honest reporting (the
    # ``garbage_collect`` helper returns no count). Same filter as
    # ``_sys_db.garbage_collect`` so the count matches the delete set.
    deleted = _count_deletable_rows(dbos_instance, cutoff_ms)

    _dbos_garbage_collect(
        dbos_instance,
        cutoff_epoch_timestamp_ms=cutoff_ms,
        rows_threshold=None,
    )

    logger.info(
        "workflow-input purge swept: deleted=%d cutoff_ms=%d retention_s=%d",
        deleted,
        cutoff_ms,
        retention_s,
    )
    return cutoff_ms, deleted


def _count_deletable_rows(dbos_instance: object, cutoff_ms: int) -> int:
    """Count workflow_status rows that the next sweep will delete.

    Mirrors the filter in ``dbos._sys_db.garbage_collect`` — older than
    ``cutoff_ms`` AND status NOT IN the in-flight set. Best-effort: a
    failure here logs and returns 0, leaving the actual delete to
    proceed — observability must not block the privacy purge.
    """
    try:
        sys_db = getattr(dbos_instance, "_sys_db", None)
        if sys_db is None:
            return 0
        engine = getattr(sys_db, "engine", None)
        if engine is None:
            return 0
        with engine.begin() as conn:
            row = conn.exec_driver_sql(
                "SELECT count(*) FROM dbos.workflow_status "
                "WHERE created_at < %s "
                "AND status NOT IN ('PENDING', 'ENQUEUED', 'DELAYED')",
                (cutoff_ms,),
            ).fetchone()
        if row is None:
            return 0
        return int(row[0])
    except Exception:  # noqa: BLE001 — observability-only count
        logger.debug(
            "workflow-input purge: deletable-row count failed; "
            "purge proceeds without a count",
            exc_info=True,
        )
        return 0


@DBOS.scheduled(_PURGE_CRON)
def purge_workflow_inputs_scheduled(
    scheduled_time: datetime, actual_time: datetime
) -> None:
    """Scheduled DBOS workflow — sweep terminal workflow inputs.

    Cadence: every 30 minutes (``*/30 * * * *``). The body delegates to
    ``purge_terminal_workflow_inputs`` so the same logic is callable
    from tests / admin scripts without rescheduling.

    Returns ``None`` per the @DBOS.scheduled function-signature
    contract. Failures inside ``purge_terminal_workflow_inputs`` are
    already logged + best-effort; a raise here would mark the
    scheduled workflow as ERROR and stop further invocations until
    DBOS recovery flips it back, so wrap to be safe.
    """
    try:
        purge_terminal_workflow_inputs()
    except Exception:  # noqa: BLE001 — scheduler poller must keep firing
        logger.exception("workflow-input purge sweep raised; cadence continues")
