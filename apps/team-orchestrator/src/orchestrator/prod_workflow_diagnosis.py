"""VT-634 phase 1 — DIAGNOSE a failed/orphaned workflow. Read-only, effect-state first.

Fazal's requirement (2026-07-10), in order: **contain**, then **diagnose** (a separate process),
then **surface on the VTR console** for a human to act. This module is the diagnosis half. It is
deliberately, structurally READ-ONLY: it opens no transaction that writes, and it never touches
DBOS. Containment is a different mechanism and a different change; a diagnosis that quietly
"fixed" things would be the silent auto-resolution the spec forbids.

WHY EFFECT-STATE COMES FIRST
----------------------------
The spec calls it the hard part and it is: a workflow that sent to SOME customers and then died
must not be blindly re-run (double-send to real people — the trust-breaker) and must not be
silently disabled (half a campaign delivered, owner unaware). You cannot choose between those
without knowing what already went out. So every finding leads with the effect-state, computed
from the ledger rather than inferred from the workflow's status:

    intended = campaign_recipients   (who the campaign was for)
    actual   = campaign_messages     (who was actually messaged, append-only audit ledger)

`campaign_messages` is append-only and survives customer deletion by design (mig 049), which is
exactly what makes it trustworthy here — it is the one record a crash cannot have rolled back.

WHAT THIS DOES NOT DO
---------------------
No containment, no re-run, no cancel, no repair. It returns findings. The VTR acts. Where the
recommendation is anything other than "no effect", the recommendation is explicitly *not* a
licence to automate — `requires_human` is set and stays set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: A workflow younger than this may simply be in flight. Matches the orphan-reaper's
#: conservative 1h floor (>> the 30-min run-control max hold + the 6-min invoke timeout), so a
#: live run or a row DBOS is mid-recovery on is never diagnosed as failed.
_FAILED_AGE_HOURS = 1

#: DBOS statuses worth diagnosing. ERROR = it failed. PENDING past the age floor = orphaned (its
#: executor died; a redeploy changed app_version so DBOS recovery will never pick it up).
_DIAGNOSABLE = ("ERROR", "PENDING")

#: send_status values in `campaign_messages` that mean a message REACHED a customer. The others
#: ('window_closed', 'rate_limited', 'error') are recorded attempts that did NOT deliver — and
#: counting them as sent would be the dangerous direction of wrong: it would make a campaign look
#: more complete than it is and hide un-messaged customers from the remainder.
_DELIVERED = ("sent", "template_sent")


class DiagnosisUnavailable(RuntimeError):
    """The diagnosis could not run. Distinct from "there is nothing to report", because a console
    that renders those two the same way tells a VTR everything is fine while it is blind."""


@dataclass(frozen=True)
class EffectState:
    """What a campaign actually did to real people, from the ledger."""

    campaign_id: str
    intended: int
    delivered: int
    attempted_not_delivered: int

    @property
    def remainder(self) -> int:
        return max(0, self.intended - self.delivered)

    @property
    def kind(self) -> str:
        if self.delivered == 0:
            return "no_effect"
        if self.remainder > 0:
            return "partial_send"
        return "complete"


@dataclass(frozen=True)
class WorkflowFinding:
    """One diagnosed workflow, in the shape the VTR console renders."""

    workflow_uuid: str
    dbos_status: str
    tenant_id: str | None
    task_id: str | None
    task_status: str | None
    terminal_outcome: str | None
    effects: list[EffectState] = field(default_factory=list)
    error_summary: str | None = None

    @property
    def effect_kind(self) -> str:
        """The WORST effect across the workflow's campaigns — a single partial send dominates,
        because it is the case that forbids both re-running and disabling."""
        kinds = {e.kind for e in self.effects}
        if "partial_send" in kinds:
            return "partial_send"
        if "complete" in kinds:
            return "complete"
        return "no_effect"

    @property
    def requires_human(self) -> bool:
        """True whenever anything already reached a customer. Deliberately not a judgement call:
        once a real person has been messaged, a machine must not decide what happens next."""
        return self.effect_kind != "no_effect"

    @property
    def recommended_action(self) -> str:
        if self.effect_kind == "partial_send":
            return (
                "CONTAIN — do not re-run (would re-message customers who already received it) "
                f"and do not disable (the remainder of {sum(e.remainder for e in self.effects)} "
                "customer(s) was never messaged). VTR resolves the remainder."
            )
        if self.effect_kind == "complete":
            return (
                "CONTAIN — every intended customer was already messaged. Re-running would "
                "double-send. Settle the workflow; no remainder to finish."
            )
        return (
            "SAFE TO CANCEL — the ledger shows nothing reached a customer. No double-send risk. "
            "Cancelling loses no delivered work."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow_uuid": self.workflow_uuid,
            "dbos_status": self.dbos_status,
            "tenant_id": self.tenant_id,
            "task_id": self.task_id,
            "task_status": self.task_status,
            "terminal_outcome": self.terminal_outcome,
            "effect_kind": self.effect_kind,
            "requires_human": self.requires_human,
            "recommended_action": self.recommended_action,
            "error_summary": self.error_summary,
            "effects": [
                {
                    "campaign_id": e.campaign_id,
                    "intended": e.intended,
                    "delivered": e.delivered,
                    "attempted_not_delivered": e.attempted_not_delivered,
                    "remainder": e.remainder,
                    "kind": e.kind,
                }
                for e in self.effects
            ],
        }


def _service_pool(pool: Any) -> Any:
    if pool is not None:
        return pool
    from orchestrator.graph import get_pool  # lazy — heavy import chain

    return get_pool()


def _parse_workflow_uuid(workflow_uuid: str) -> tuple[str | None, str | None]:
    """``manager_task:{tenant_id}:{task_id}`` → (tenant_id, task_id).

    Returns ``(None, None)`` for any other shape rather than guessing. A mis-parsed tenant id
    would attribute one tenant's sends to another, which on this path means telling a VTR that
    the wrong customers were messaged.
    """
    parts = workflow_uuid.split(":")
    if len(parts) < 3 or parts[0] != "manager_task":
        return None, None
    return parts[1] or None, parts[2] or None


def _effect_states(conn: Any, tenant_id: str, task_id: str | None) -> list[EffectState]:
    """Ledger-derived effect state for every campaign this tenant has in flight for the task.

    Goes through ``CampaignsWrapper`` on an RLS-scoped tenant connection, NOT a service-role
    cross-tenant read. The first cut used the service pool and raw SQL and was correctly blocked
    by the VT-72/306 no-direct-tenant-db-access gate. The gate was right: this read is per-tenant
    (the tenant id comes out of the workflow uuid), so there is no reason to bypass RLS, and "it
    is an ops tool" is not one. Being an ops tool is a reason to be MORE careful about which
    tenant's customer data is visible, not less.
    """
    from orchestrator.db.wrappers import CampaignsWrapper

    rows = CampaignsWrapper().effect_state_rollup(
        tenant_id, delivered_statuses=_DELIVERED, conn=conn,
    )
    out: list[EffectState] = []
    for row in rows:
        intended = int(row.get("intended") or 0)
        delivered = int(row.get("delivered") or 0)
        attempted = int(row.get("attempted") or 0)
        # A campaign nobody was ever added to and nobody was messaged from is noise, not evidence.
        if not intended and not delivered and not attempted:
            continue
        out.append(EffectState(
            campaign_id=str(row.get("campaign_id")), intended=intended,
            delivered=delivered, attempted_not_delivered=attempted,
        ))
    return out


def diagnose_failed_workflows(
    *, pool: Any = None, age_hours: int = _FAILED_AGE_HOURS, limit: int = 100,
) -> list[WorkflowFinding]:
    """Diagnose failed/orphaned ``manager_task`` workflows. READ-ONLY. Never raises.

    Returns findings ordered worst-first (anything that already reached a customer comes before
    anything that did not), because that is the order a VTR should work them.
    """
    findings: list[WorkflowFinding] = []
    try:
        sysconn_rows = _read_dbos_rows(pool, age_hours, limit)
    except Exception as exc:
        # RAISED, not swallowed into an empty list. An empty result renders on the console as
        # "no failed workflows" — the single most dangerous thing this module could say when it
        # is actually broken. The caller must be able to tell "nothing is wrong" apart from
        # "I cannot see whether anything is wrong".
        logger.warning("VT-634 diagnosis: could not read DBOS workflow_status", exc_info=True)
        raise DiagnosisUnavailable(f"DBOS workflow_status unreadable: {type(exc).__name__}") from exc

    # No service-role connection is opened here. Each workflow's tenant id comes out of its own
    # uuid, so every read below is per-tenant and goes through the RLS-scoped accessors.
    from orchestrator.manager import task_store

    for workflow_uuid, status, error_text in sysconn_rows:
        tenant_id, task_id = _parse_workflow_uuid(workflow_uuid)
        task_status = terminal_outcome = None
        effects: list[EffectState] = []
        if tenant_id:
            try:
                if task_id:
                    trow = task_store.get_task(tenant_id, task_id)
                    if trow:
                        task_status = trow.get("status")
                        terminal_outcome = trow.get("terminal_outcome")
                effects = _effect_states(None, tenant_id, task_id)
            except Exception:  # noqa: BLE001 — one unreadable tenant must not hide the others
                logger.warning(
                    "VT-634 diagnosis: per-tenant read failed for %s — reporting the workflow "
                    "WITHOUT effect-state (treat as unknown, never as no_effect)",
                    workflow_uuid, exc_info=True,
                )
                # Deliberately surfaced rather than dropped: a workflow we cannot read the effects
                # of is exactly the one a human needs to look at. The error text carries the fact
                # so it cannot be mistaken for a clean "nothing was sent".
                error_text = f"{error_text or ''} [effect-state UNREADABLE]".strip()
        findings.append(WorkflowFinding(
            workflow_uuid=workflow_uuid, dbos_status=status, tenant_id=tenant_id,
            task_id=task_id, task_status=task_status, terminal_outcome=terminal_outcome,
            effects=effects, error_summary=error_text,
        ))

    severity = {"partial_send": 0, "complete": 1, "no_effect": 2}
    findings.sort(key=lambda f: severity.get(f.effect_kind, 3))
    if findings:
        logger.warning(
            "VT-634 diagnosis: %d failed/orphaned workflow(s); %d already reached a customer",
            len(findings), sum(1 for f in findings if f.requires_human),
        )
    return findings


def _read_dbos_rows(pool: Any, age_hours: int, limit: int) -> list[tuple[str, str, str | None]]:
    """Read the DBOS system DB. Separate function so the main path can be tested without one.

    The DSN is derived from the POOL's own conninfo rather than re-read from config or env, so
    this can never diagnose a different database than the one the app is running against. (The
    first cut imported a ``orchestrator.config.settings`` that does not exist; the ImportError was
    swallowed by the caller's ``except`` and the module would have returned "no failed workflows"
    forever — a diagnosis tool that fails silently is worse than no diagnosis tool.)

    ``created_at`` in ``dbos.workflow_status`` is epoch MILLISECONDS, not a timestamptz — comparing
    it to ``now()`` directly silently matches nothing.
    """
    import re

    import psycopg

    dsn = getattr(_service_pool(pool), "conninfo", "") or ""
    if not dsn:
        raise DiagnosisUnavailable("no pool conninfo — cannot locate the DBOS system DB")
    sysdsn = re.sub(r"/([^/?]+)(\?|$)", r"/postgres_dbos_sys\2", dsn, count=1)
    cutoff_ms_expr = "(EXTRACT(EPOCH FROM now()) - %s * 3600) * 1000"
    with psycopg.connect(sysdsn, autocommit=True, connect_timeout=10) as sc:
        rows = sc.execute(
            "SELECT workflow_uuid, status, error "
            "FROM dbos.workflow_status "
            f"WHERE status = ANY(%s) AND workflow_uuid LIKE 'manager_task:%%' "
            f"  AND created_at < {cutoff_ms_expr} "
            "ORDER BY created_at DESC LIMIT %s",
            (list(_DIAGNOSABLE), age_hours, limit),
        ).fetchall()
    return [(str(r[0]), str(r[1]), (str(r[2]) if r[2] is not None else None)) for r in rows]


__all__ = [
    "DiagnosisUnavailable",
    "EffectState",
    "WorkflowFinding",
    "diagnose_failed_workflows",
]
