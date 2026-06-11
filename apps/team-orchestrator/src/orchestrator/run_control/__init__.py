"""VT-374 run-control executor — pause / override / sweep primitives (plan §4).

Two-tier pause semantics (F9/N4), consume-first one-shot overrides (F8/N2) and
the allowed-keys pinned-input merge (F6/I7). Seam wiring lives at the call
sites (runner/coordinator/executors); this package only provides the
primitives, so the gate manifest + registry stay the single source of truth.

DEP-LESS IMPORT CONTRACT: this package (and ``registry``/``gate_manifest``)
must import with stdlib only — the dep-less CI smoke imports the manifest.
Everything heavy (orchestrator.graph pool, observability log_event, dbos) is
imported lazily inside the function that needs it. ``rerun`` is deliberately
NOT imported here — import ``orchestrator.run_control.rerun`` directly.

DB posture: ``workflow_controls`` / ``step_overrides`` are deny-all RLS
(FORCE, zero policies) — access goes through the SERVICE pool
(``orchestrator.graph.get_pool``), the same posture ``run_controls`` had
(run_control_handler precedent), never ``tenant_connection`` (``app_role``
would be denied on the zero-policy tables). ``pool`` is injectable for tests,
mirroring the house ``tenant_connection`` pattern.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from orchestrator.run_control.registry import (
    KIND_RERUN_POLICY,
    REGISTRY,
    RERUNNABLE,
    STEP_IMPL_MODULES,
    WORKFLOW_KINDS,
    StepEntry,
)

logger = logging.getLogger(__name__)

_DEFAULT_POLL_S = 5.0
# Durable polls checkpoint one DBOS step per read — 15s keeps a long pause from
# flooding the DBOS system tables while staying responsive enough for an ops release.
_DURABLE_POLL_S = 15.0

# (tenant_id, workflow_kind) -> last successfully READ pause state. The F9 two-tier
# guarantee: once a pause has been observed for a scope, a control-read error fails
# CLOSED for that scope; scopes with no known pause fail OPEN + degraded alert.
# Per-process and empty on boot (N4) — warm_pause_cache() restores it best-effort,
# so the fail-closed guarantee is best-effort-after-restart by design.
_KNOWN_PAUSED: dict[tuple[str, str], bool] = {}


@dataclass(frozen=True)
class Override:
    """One consumed ``step_overrides`` row (pins already redacted at write, §5)."""

    id: UUID
    tenant_id: UUID
    workflow_kind: str
    step_name: str
    workflow_id: UUID | None
    pinned_input: dict[str, Any] | None
    pinned_output: dict[str, Any] | None
    reason: str | None
    created_by: UUID | None
    created_at: datetime | None
    expires_at: datetime | None
    consumed_at: datetime | None
    consumed_run_id: UUID | None


def _require_kind(workflow_kind: str) -> None:
    """A typo'd kind would silently never match a pause row — fail loud instead.

    Raised OUTSIDE check_pause's fail-open handler: a programming error must never
    be laundered into 'not paused'.
    """
    if workflow_kind not in WORKFLOW_KINDS:
        raise ValueError(f"unknown workflow_kind {workflow_kind!r} (not in WORKFLOW_KINDS)")


def _service_pool(pool: Any) -> Any:
    if pool is not None:
        return pool
    from orchestrator.graph import get_pool  # lazy — heavy (langgraph) import chain

    return get_pool()


def is_paused(tenant_id: UUID | str, workflow_kind: str, *, pool: Any = None) -> bool:
    """Raw control read — raises on DB error. ``check_pause`` wraps with F9 semantics.

    Active pause = an unreleased workflow_controls row for (tenant, kind). Every
    successful read refreshes the known-state cache.
    """
    _require_kind(workflow_kind)
    with _service_pool(pool).connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM workflow_controls "
            "WHERE tenant_id = %s AND workflow_kind = %s AND released_at IS NULL "
            "LIMIT 1",
            (str(tenant_id), workflow_kind),
        ).fetchone()
    paused = row is not None
    _KNOWN_PAUSED[(str(tenant_id), workflow_kind)] = paused
    return paused


def check_pause(tenant_id: UUID | str, workflow_kind: str, *, pool: Any = None) -> bool:
    """Two-tier pause check (F9/N4). Never raises on control-read errors.

    - read succeeds → truth (cache refreshed).
    - read fails + the cache says this scope was paused → fail CLOSED (True).
    - read fails + no known pause → fail OPEN (False) + ``run_control_degraded``
      structured log_event, so a silent control outage is alert-visible.
    """
    _require_kind(workflow_kind)
    key = (str(tenant_id), workflow_kind)
    try:
        return is_paused(tenant_id, workflow_kind, pool=pool)
    except Exception:  # noqa: BLE001 — control-read failure routes to the two-tier posture
        if _KNOWN_PAUSED.get(key):
            logger.warning(
                "run_control: control read failed; acknowledged pause cached for "
                "tenant=%s kind=%s — fail CLOSED (treated paused)",
                key[0],
                key[1],
                exc_info=True,
            )
            return True
        logger.warning(
            "run_control: control read failed; no known pause for tenant=%s kind=%s "
            "— fail OPEN (degraded)",
            key[0],
            key[1],
            exc_info=True,
        )
        _emit_degraded(key)
        return False


def _emit_degraded(key: tuple[str, str]) -> None:
    """Best-effort ``run_control_degraded`` event — the alert substrate scans pipeline_log.

    log_event itself never raises, but its import chain is heavy (graph/pool), so the
    whole emission is guarded: a logging failure must never alter pause semantics.
    """
    try:
        from orchestrator.observability.log import log_event  # lazy — heavy import chain

        log_event(
            event_type="run_control_degraded",
            run_id=uuid4(),
            tenant_id=key[0],
            severity="error",
            component="run_control",
            payload={"workflow_kind": key[1], "posture": "fail_open"},
        )
    except Exception:  # noqa: BLE001 — observability must not break the control path
        logger.warning("run_control: degraded-event emission failed", exc_info=True)


def warm_pause_cache(*, pool: Any = None) -> None:
    """Boot-time best-effort cache warm (N4) — called from main.py startup.

    Loads every active hold so a post-restart control-read error still fails CLOSED
    for scopes that were paused before the restart. Never raises (a warm failure must
    not block worker boot); the degraded alert path still covers the gap.
    """
    try:
        with _service_pool(pool).connection() as conn:
            rows = conn.execute(
                "SELECT tenant_id, workflow_kind FROM workflow_controls "
                "WHERE released_at IS NULL"
            ).fetchall()
        for row in rows:
            if isinstance(row, dict):
                tenant, kind = row["tenant_id"], row["workflow_kind"]
            else:
                tenant, kind = row[0], row[1]
            _KNOWN_PAUSED[(str(tenant), str(kind))] = True
        logger.info("run_control: pause cache warmed (%d active holds)", len(rows))
    except Exception:  # noqa: BLE001 — best-effort by design (N4)
        logger.warning("run_control: pause-cache warm failed (best-effort)", exc_info=True)


def hold_while_paused(
    tenant_id: UUID | str,
    workflow_kind: str,
    *,
    sleep_fn: Callable[[float], Any] = time.sleep,
    poll_s: float = _DEFAULT_POLL_S,
    on_hold: Callable[[int], Any] | None = None,
    pool: Any = None,
) -> int:
    """Plain-code seam hold: block while (tenant, kind) is paused; return paused_ms.

    Returns 0 when never held. ``on_hold(paused_ms_so_far)`` fires once per poll while
    held (best-effort — seams use it for status logging / Cowork nudges). For seams
    INSIDE a DBOS workflow body use :func:`hold_while_paused_durable` instead — a bare
    in-loop sleep there is not recovery-safe.
    """
    if not check_pause(tenant_id, workflow_kind, pool=pool):
        return 0
    start = time.monotonic()
    while True:
        if on_hold is not None:
            try:
                on_hold(int((time.monotonic() - start) * 1000))
            except Exception:  # noqa: BLE001 — a status callback must never break the hold
                logger.warning("run_control: on_hold callback failed", exc_info=True)
        sleep_fn(poll_s)
        if not check_pause(tenant_id, workflow_kind, pool=pool):
            return int((time.monotonic() - start) * 1000)


def _pause_read_body(tenant_id: str, workflow_kind: str) -> bool:
    """Plain body for the durable poll's ``@DBOS.step`` — module-level so the step
    keeps a stable qualname for DBOS recovery; decorated lazily in
    :func:`hold_while_paused_durable` so import stays dep-less."""
    return check_pause(tenant_id, workflow_kind)


_durable_pause_read: Callable[[str, str], bool] | None = None


def hold_while_paused_durable(tenant_id: UUID | str, workflow_kind: str) -> int:
    """DBOS-workflow-body hold (N3 seam): checkpointed wait, never a bare sleep.

    Each control read is its OWN ``@DBOS.step`` and the wait between polls is
    ``DBOS.sleep`` — both checkpointed, so a paused run survives a worker restart and
    resumes the hold (acceptance §10.2). ``paused_ms`` is counted from poll intervals
    (deterministic under DBOS replay), not wall-clock. dbos imports lazily INSIDE this
    function so the module stays importable dep-less.
    """
    from dbos import DBOS  # lazy — only DBOS-workflow callers reach here

    global _durable_pause_read
    if _durable_pause_read is None:
        _durable_pause_read = DBOS.step()(_pause_read_body)
    paused_ms = 0
    while _durable_pause_read(str(tenant_id), str(workflow_kind)):
        DBOS.sleep(_DURABLE_POLL_S)
        paused_ms += int(_DURABLE_POLL_S * 1000)
    return paused_ms


def consume_override(
    conn: Any,
    *,
    tenant_id: UUID | str,
    workflow_kind: str,
    step_name: str,
    run_id: UUID | str,
) -> Override | None:
    """Consume-first one-shot override claim (F8) — recovery-idempotent (N2/A5).

    Single-statement claim: the subquery takes ``FOR UPDATE SKIP LOCKED`` and the
    UPDATE stamps ``consumed_at``/``consumed_run_id`` atomically — race-safe even on
    an autocommit service-pool connection (two racing runs: one consumes, one
    proceeds clean). The N2 re-apply arm is ``consumed_run_id = run_id`` standing
    ALONE: DBOS recovery re-entering a workflow body after the consume txn committed
    re-applies the SAME override even past its expiry or a later cancellation (the
    pin already governed the run's first execution — determinism wins), and the
    re-apply row WINS ordering over any fresh unconsumed pin (``IS TRUE`` forces the
    NULL-consumed rows below it; bare ``boolean DESC`` would sort NULLs first).
    ``COALESCE`` keeps the original ``consumed_at`` on re-apply.

    ``conn`` is the caller's SERVICE-pool connection (step_overrides is deny-all
    RLS). The caller passes the run identity its workflow actually carries — the
    ``workflow_id IS NULL OR workflow_id = run_id`` arm matches both next-run pins
    and run-targeted pins.
    """
    if (workflow_kind, step_name) not in REGISTRY:
        raise ValueError(f"unknown step ({workflow_kind!r}, {step_name!r})")
    row = conn.execute(
        """
        UPDATE step_overrides
           SET consumed_at = COALESCE(consumed_at, now()),
               consumed_run_id = %(run_id)s
         WHERE id = (
             SELECT id FROM step_overrides
              WHERE tenant_id = %(tenant_id)s
                AND workflow_kind = %(workflow_kind)s
                AND step_name = %(step_name)s
                AND (workflow_id IS NULL OR workflow_id = %(run_id)s)
                AND (
                      (consumed_at IS NULL AND cancelled_at IS NULL
                       AND (expires_at IS NULL OR expires_at > now()))
                      OR consumed_run_id = %(run_id)s
                    )
              ORDER BY (consumed_run_id = %(run_id)s) IS TRUE DESC, created_at ASC
              LIMIT 1
              FOR UPDATE SKIP LOCKED
         )
         RETURNING id, workflow_id, pinned_input, pinned_output, reason,
                   created_by, created_at, expires_at, consumed_at, consumed_run_id
        """,
        {
            "run_id": str(run_id),
            "tenant_id": str(tenant_id),
            "workflow_kind": workflow_kind,
            "step_name": step_name,
        },
    ).fetchone()
    if row is None:
        return None
    if not isinstance(row, dict):
        cols = (
            "id",
            "workflow_id",
            "pinned_input",
            "pinned_output",
            "reason",
            "created_by",
            "created_at",
            "expires_at",
            "consumed_at",
            "consumed_run_id",
        )
        row = dict(zip(cols, row, strict=True))
    return Override(
        id=_as_uuid(row["id"]),
        tenant_id=_as_uuid(tenant_id),
        workflow_kind=workflow_kind,
        step_name=step_name,
        workflow_id=_as_uuid_or_none(row["workflow_id"]),
        pinned_input=row["pinned_input"],
        pinned_output=row["pinned_output"],
        reason=row["reason"],
        created_by=_as_uuid_or_none(row["created_by"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
        consumed_run_id=_as_uuid_or_none(row["consumed_run_id"]),
    )


def _as_uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _as_uuid_or_none(value: Any) -> UUID | None:
    return None if value is None else _as_uuid(value)


def apply_pinned_input(
    entry: StepEntry, base: dict[str, Any], pinned: dict[str, Any]
) -> dict[str, Any]:
    """Deep-merge ``pinned`` over ``base``, restricted to ``entry.allowed_keys`` (F6/I7).

    A pinned top-level key outside the allow-list raises ``ValueError`` (the ops API
    maps it to 422; a seam reaching here with a bad key is a bug — fail loud, never
    silently drop a pin). Returns a NEW dict; neither input is mutated. Nested dicts
    merge recursively under an allowed key; any other value replaces.
    """
    illegal = set(pinned) - set(entry.allowed_keys)
    if illegal:
        raise ValueError(
            f"pinned_input keys {sorted(illegal)!r} not allow-listed for "
            f"({entry.workflow_kind}, {entry.step_name}); "
            f"allowed={sorted(entry.allowed_keys)!r}"
        )
    return _deep_merge(base, pinned)


def _deep_merge(base: dict[str, Any], pinned: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in pinned.items():
        current = out.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            out[key] = _deep_merge(current, value)
        else:
            out[key] = value
    return out


def expire_overrides_sweep(*, pool: Any = None) -> int:
    """Cancel stale unconsumed override rows; return how many were cancelled.

    Two hygiene legs, both restricted to unconsumed + uncancelled pins:

    1. EXPIRED pins (F8): ``expires_at <= now()``. NULL-workflow (next-run) pins
       REQUIRE ``expires_at`` — this is what makes that bound real (an expired pin
       must never fire on a much-later run). Run-targeted pins carrying an expiry
       are swept on the same predicate.
    2. ORPHANED run-BOUND pins (VT-375 hygiene): ``workflow_id`` points at a
       ``pipeline_runs`` row that reached a TERMINAL status WITHOUT the pin being
       consumed. The run will never re-execute, so the pin can never fire; cancel
       it so it does not linger un-cancelled forever (an expiry-less run-bound pin
       would otherwise survive every expiry sweep). Same count semantics — both
       legs contribute to the single returned cancelled count.

       TERMINAL here = NOT IN ('running', 'paused'). 'paused' is NOT terminal —
       it is RESUMABLE: runner.py:285 stamps 'paused' when run-control parks a run,
       and approval_resume / close_webhook_run later drives the SAME run onward to
       'completed' (the migration-052 CHECK lists 'paused' as a distinct value
       precisely because the run is not finished). A pin bound to a paused run may
       still fire when that run resumes, so the orphan leg must SPARE it; only the
       genuinely-finished statuses (completed / escalated / aborted_hard_limit /
       duplicate_rejected) leave a run-bound pin un-fireable.

    Run in ONE UPDATE so the rowcount is the true total (a pin matching both legs
    is counted once). Service pool only (step_overrides is deny-all RLS).
    """
    with _service_pool(pool).connection() as conn:
        cur = conn.execute(
            "UPDATE step_overrides o SET cancelled_at = now() "
            "WHERE o.cancelled_at IS NULL AND o.consumed_at IS NULL "
            "AND ("
            "  (o.expires_at IS NOT NULL AND o.expires_at <= now())"
            "  OR ("
            "    o.workflow_id IS NOT NULL"
            "    AND EXISTS ("
            "      SELECT 1 FROM pipeline_runs r"
            "       WHERE r.id = o.workflow_id "
            "         AND r.status NOT IN ('running', 'paused')"
            "    )"
            "  )"
            ")"
        )
        cancelled = cur.rowcount or 0
    if cancelled:
        logger.info(
            "run_control: override sweep cancelled %d row(s) (expired + orphaned run-bound)",
            cancelled,
        )
    return cancelled


__all__ = [
    "KIND_RERUN_POLICY",
    "REGISTRY",
    "RERUNNABLE",
    "STEP_IMPL_MODULES",
    "WORKFLOW_KINDS",
    "Override",
    "StepEntry",
    "apply_pinned_input",
    "check_pause",
    "consume_override",
    "expire_overrides_sweep",
    "hold_while_paused",
    "hold_while_paused_durable",
    "is_paused",
    "warm_pause_cache",
]
