"""DSR deletion fulfillment — tenant-wide subject data purge (VT-DSRPurge).

The DPDP / privacy regime promises Right-to-Erasure: a subject can
request deletion and the controller MUST honor it. Until this module
landed, ``dsr_handler.py`` only acknowledged tickets — no code path set
``dsr_tickets.status = 'completed'`` because no code actually deleted
anything. This module closes that gap for **tenant-wide** subject
requests (the tenant IS the DSR subject for the Viabe-Team product;
end-customer data lives inside the tenant's records and goes with it).

Privileged path — documented
----------------------------
The brief asked for the purge to run under ``tenant_connection``
(``SET ROLE app_role`` + tenant GUC). Migration 015's ``app_role``
grants enumerate only seven tables explicitly (``tenants``,
``pipeline_runs``, ``pipeline_steps``, ``phase_transitions``,
``twilio_inbound_events``, ``dsr_tickets``, ``rate_limit_buckets``)
plus ``ALTER DEFAULT PRIVILEGES`` which catches tables created AFTER
015. Three tables this purge must touch — ``subscriptions``,
``phone_token_resolutions``, ``privacy_audit_log`` — were created
BEFORE 015 and are NOT in the explicit grant list. Running the purge
under ``tenant_connection`` therefore fails with
``InsufficientPrivilege`` on those tables.

Two paths were considered:

  (a) Add a permission migration granting app_role the missing
      DELETE / INSERT. **Rejected** — would also let every normal
      tenant-scoped writer DELETE rows from privacy_audit_log
      (DPDP-required append-only) and from billing surfaces. Wider
      blast radius than this PR needs.

  (b) Run the purge under the privileged service role (the bare pool
      connection, which has BYPASSRLS in CI + Supabase production)
      and rely on explicit ``WHERE tenant_id = %s`` predicates for
      scoping. **Chosen.** DSR fulfillment IS architecturally an
      admin / operator action — the controller acting on a subject
      request, not a tenant client writing. The explicit predicate
      keeps the scope tight.

Architecture
------------
- Single public entry point: ``purge_tenant_data(ticket_id)``. Takes
  a ``dsr_tickets.id``, reads the ticket's ``tenant_id``, and runs
  the purge in FK-safe order within a single transaction on a
  privileged pool connection.
- Per-table delete helpers live in this module so the inventory + the
  delete order are reviewable in ONE file (the brief explicitly asks
  for the inventory to be a reviewable artifact).
- Idempotent: a ticket already marked ``status='completed'`` short-
  circuits before any DELETE runs.

Tombstone policy (DRAFT — Fazal-overridable)
-------------------------------------------
- ``tenants`` row: kept, anonymized (``business_name='[deleted]'``,
  ``whatsapp_number=NULL``, ``opt_out=true``). Hard-deleting the row
  would break FK constraints from ``privacy_audit_log`` and
  ``dsr_tickets`` — both are required retention surfaces.
- ``dsr_tickets``: the requesting ticket stays. Status flips to
  ``'completed'``, ``completed_at`` set. This is the regulator-facing
  evidence the request was honored.
- ``privacy_audit_log``: untouched (DPDP 7-year retention). A
  ``subject_data_purged`` event is appended FIRST, before any other
  DELETE, so the audit captures the request even on partial failure.
- All other tenant-scoped tables: hard DELETE.

DBOS layer — out of scope for synchronous deletion
--------------------------------------------------
``dbos.workflow_status`` and the cascaded operation_outputs / step
tables are framework-managed and NOT indexed by ``tenant_id``. PR #47
(``orchestrator.dbos_purge``) is time-based — terminal workflows age
out in ~2h. Per-tenant deletion is not expressible via the framework
helper; subject data persists in DBOS for at most the retention
window after the tenant's last completed workflow, then GCs naturally.
The privacy notice must reflect this. Do not raw-SQL DELETE against
DBOS tables here.

Razorpay webhook events — out of scope
--------------------------------------
``razorpay_webhook_events`` is workspace-wide (no ``tenant_id`` column;
deny-all RLS). The JSONB payload may carry tenant linkage but
payload-level scrub is its own data-migration row.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from orchestrator.graph import get_pool
from orchestrator.observability.audit_log import log_privacy_event

logger = logging.getLogger(__name__)

# VT-93 / Cowork-escalation 20260605T100800Z — refund_executions DSR mode. Default
# HARD-DELETE (DPDP right-to-erasure). Set TEAM_REFUND_RETAIN_ON_DSR=1 to switch to
# anonymize-retain: keep total_refund_paise + completed_at (Indian tax/accounting
# retention may require refund amount+date 6-8 yrs even after an erasure request)
# and scrub the Razorpay vendor detail. A config flip, not a refactor — Fazal's /
# legal's ruling sets it. NEEDS-FAZAL: confirm the retention requirement.
_REFUND_HARD_DELETE_ON_DSR = os.environ.get("TEAM_REFUND_RETAIN_ON_DSR", "0") != "1"


# Anonymization values for the tenants row tombstone. Kept here (not
# inlined) so a Fazal-driven policy change touches one constant set.
#
# VT-160: EVERY identifying column on tenants must be irreversibly scrubbed —
# NULL (not a predictable token) for the PII anchors. The earlier set covered
# only business_name/whatsapp_number; owner_phone (mig 050 — globally-UNIQUE-
# indexed, the strongest re-id anchor), owner_contact (mig 066) and locality
# (mig 001) were left intact, leaving the subject re-identifiable post-deletion
# (DPDP-incomplete). city_tier is deliberately NOT scrubbed — it is already
# coarsened (tier_1/2/3), not PII. opt_out is the operational flag, not PII.
_TENANT_ANONYMIZE = {
    "business_name": "[deleted]",
    "whatsapp_number": None,
    "owner_phone": None,  # mig 050 — globally-unique re-id anchor
    "owner_contact": None,  # mig 066
    "locality": None,  # mig 001 — geographic identifier
    "opt_out": True,
}

# FK-safe deletion order. Children before parents — pipeline_steps /
# campaigns / owner_inputs all FK to pipeline_runs(id); l1_relationships
# FK to l1_entities(id). Order is read top-to-bottom by the purge loop.
_PURGE_ORDER: tuple[str, ...] = (
    "l1_relationships",
    "l1_entities",
    # VT-323: L2 episodic memory. Leaf (references tenants — anonymized, NOT
    # deleted — and no child tables point at it), so order-insensitive. payload
    # CAN carry PII at rest, and there is NO ON DELETE CASCADE + no other
    # hard-delete path (retention only soft-deletes, reconstitution only
    # sentinels), so a tenant DSR-delete MUST sweep it here or the whole L2
    # episodic store survives the purge.
    "episodic_events",
    # VT-325: per-listing platform source. Leaf (FK tenants only — anonymized,
    # NOT deleted, so the ON DELETE CASCADE never fires on a DSR), order-insensitive.
    # MUST be swept here or a tenant's listings survive the purge (the VT-323
    # episodic_events lesson, on a fresh table).
    "platform_listings",
    # VT-92: day-39 decision audit. Leaf (FK tenants only). Carries the tenant's
    # ARRR/fees (subject billing data) → hard-deleted on DSR (CASCADE never fires on
    # a DSR-anonymize). Order-insensitive.
    "day39_evaluations",
    # VT-93: refund execution ledger. Leaf (FK tenants only; day39_evaluation_id
    # is an id-copy, not a FK), order-insensitive. Carries subject billing data
    # (total_refund_paise + Razorpay refund ids in refund_responses) → hard-
    # deleted on DSR; the immutable privacy_audit_log copy survives. The purge txn
    # sets orchestrator.dsr_purge_in_progress so the completed-row immutability
    # trigger (mig 099) permits this delete.
    "refund_executions",
    "founding_tier_claims",  # VT-94: per-tenant founding claim (audit) — hard-delete on DSR
    "template_error_reports",  # VT-335: owner template-error reports (PII free text) — hard-delete
    "owner_inputs",
    "campaigns",
    "pipeline_steps",
    "pipeline_runs",
    "subscriber_states",
    "phase_transitions",
    "subscriptions",
    "phone_token_resolutions",
    # VT-8.5: customer consent proof (migration 067). Leaf table (no FK in or
    # out — phone_token-keyed, no customers FK), so order-insensitive; grouped
    # with the privacy surfaces. Tenant-wide DSR must sweep it.
    "record_of_consent",
    "twilio_inbound_events",
    "rate_limit_buckets",
)


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """Per-table delete counts + completion marker.

    Returned for both telemetry + test assertions. ``ticket_id`` echoes
    the input so callers can confirm which request the result belongs
    to. ``deleted_counts`` is keyed on table name; the value is the
    number of rows the DELETE statement reported (``cur.rowcount``).
    ``tenant_anonymized`` is True only when the ``tenants`` row update
    actually ran (False on the idempotent-replay path).
    """

    ticket_id: UUID
    tenant_id: UUID
    deleted_counts: dict[str, int]
    tenant_anonymized: bool
    already_completed: bool


def purge_tenant_data(ticket_id: UUID) -> PurgeResult:
    """Fulfil a deletion DSR ticket: purge subject data, mark completed.

    Sequence:
      1. Load the ticket; resolve ``tenant_id``.
      2. If ``status='completed'`` already, return the idempotent
         no-op result.
      3. Append a ``subject_data_purged`` event to
         ``privacy_audit_log`` (BEFORE any delete, so the audit
         captures the request even on partial failure).
      4. DELETE child tables in ``_PURGE_ORDER``, with explicit
         ``WHERE tenant_id = %s`` on each.
      5. UPDATE ``tenants`` row → anonymized tombstone (kept for FK
         integrity with privacy_audit_log + dsr_tickets).
      6. UPDATE the ticket → status='completed', completed_at=now().

    Steps 3-6 run inside a single ``conn.transaction()`` on a
    privileged pool connection (see module docstring for the elevated-
    path justification). A failure mid-sequence rolls back; re-running
    re-runs the full sequence (idempotent via the status check at the
    top).
    """
    deleted_counts: dict[str, int] = {}
    tenant_anonymized = False

    with get_pool().connection() as conn:
        tenant_id = _resolve_tenant_id_or_raise(conn, ticket_id)

        with conn.transaction():
            # VT-93: exempt this purge txn from the refund_executions immutability
            # trigger so a completed refund row can be hard-deleted (DPDP right-to-
            # erasure). The durable record survives in privacy_audit_log (mig 099).
            conn.execute("SET LOCAL orchestrator.dsr_purge_in_progress = 'on'")
            if _ticket_already_completed(conn, ticket_id):
                return PurgeResult(
                    ticket_id=ticket_id,
                    tenant_id=tenant_id,
                    deleted_counts={},
                    tenant_anonymized=False,
                    already_completed=True,
                )

            _append_audit_event(conn, tenant_id, ticket_id)

            for table in _PURGE_ORDER:
                if table == "refund_executions" and not _REFUND_HARD_DELETE_ON_DSR:
                    # Anonymize-retain mode: keep amount+date (tax/accounting),
                    # scrub vendor detail. Rows are RETAINED (0 deleted). Runs
                    # under the dsr_purge_in_progress flag set above, so the
                    # completed-row immutability trigger permits the UPDATE.
                    from orchestrator.db import refund_executions as _refund_ledger

                    _refund_ledger.anonymize_retain(conn, tenant_id)
                    rows_deleted = 0
                else:
                    rows_deleted = _delete_where_tenant(conn, table, tenant_id)
                deleted_counts[table] = rows_deleted
                # VT-185 Q1 Option A: per-table audit row written AFTER
                # each successful DELETE. Combined with the intent row
                # above, total = 1 + N audit rows per purge. CL-390
                # full-granularity audit compliance.
                _append_per_table_audit(conn, tenant_id, ticket_id, table, rows_deleted)

            tenant_anonymized = _anonymize_tenant_row(conn, tenant_id)
            _mark_ticket_completed(conn, ticket_id)

    logger.info(
        "dsr_purge: ticket_id=%s tenant_id=%s deleted=%s tenant_anonymized=%s",
        ticket_id,
        tenant_id,
        deleted_counts,
        tenant_anonymized,
    )
    return PurgeResult(
        ticket_id=ticket_id,
        tenant_id=tenant_id,
        deleted_counts=deleted_counts,
        tenant_anonymized=tenant_anonymized,
        already_completed=False,
    )


def _resolve_tenant_id_or_raise(conn: Any, ticket_id: UUID) -> UUID:
    """Read the ticket's ``tenant_id``. Privileged role bypasses RLS,
    so the lookup sees the row regardless of tenant context."""
    raw = conn.execute(
        "SELECT tenant_id FROM dsr_tickets WHERE id = %s",
        (str(ticket_id),),
    ).fetchone()
    if raw is None:
        raise ValueError(f"dsr_tickets row not found: id={ticket_id}")
    row = cast("dict[str, Any]", raw)
    tenant_id_value = row["tenant_id"]
    return tenant_id_value if isinstance(tenant_id_value, UUID) else UUID(str(tenant_id_value))


def _ticket_already_completed(conn: Any, ticket_id: UUID) -> bool:
    raw = conn.execute(
        "SELECT status FROM dsr_tickets WHERE id = %s",
        (str(ticket_id),),
    ).fetchone()
    if raw is None:
        raise ValueError(f"dsr_tickets row not found inside purge transaction: id={ticket_id}")
    row = cast("dict[str, Any]", raw)
    return bool(row["status"] == "completed")


def _append_audit_event(conn: Any, tenant_id: UUID, ticket_id: UUID) -> None:
    """Append an event noting the purge intent.

    The ``privacy_audit_log`` table (008) is the regulator-required 7-year
    retention surface. VT-80 now owns the tamper-evident hash-chain:
    ``log_privacy_event`` computes the real prev/this_hash under the chain
    advisory lock (runs on this same BYPASSRLS purge transaction).
    """
    log_privacy_event(
        conn,
        tenant_id=tenant_id,
        event_type="subject_data_purged",
        payload={"ticket_id": str(ticket_id)},
        actor="dsr_purge",
    )


def _append_per_table_audit(
    conn: Any,
    tenant_id: UUID,
    ticket_id: UUID,
    table: str,
    rows_deleted: int,
) -> None:
    """VT-185 per-table audit row written AFTER each successful DELETE.

    Companion to ``_append_audit_event``'s intent row (BEFORE deletes).
    Payload carries ``{ticket_id, table, rows_deleted}`` so the regulator
    audit trail captures actual purge granularity per table — CL-390
    compliance. Q1 Option A locked per Cowork plan-review 2026-05-26.

    VT-80: appended through ``log_privacy_event`` (real hash-chain) on the same
    purge transaction — same chain as the intent row.
    """
    log_privacy_event(
        conn,
        tenant_id=tenant_id,
        event_type="subject_data_purged_table",
        payload={
            "ticket_id": str(ticket_id),
            "table": table,
            "rows_deleted": rows_deleted,
        },
        actor="dsr_purge",
    )


def purge_tenant_data_dry_run(ticket_id: UUID) -> PurgeResult:
    """VT-185 dry-run: count rows that WOULD be deleted; commit nothing.

    Mirrors ``purge_tenant_data`` discovery flow but substitutes
    ``SELECT COUNT(*)`` for each ``DELETE``, and skips audit + ticket-
    completion writes entirely. The returned ``PurgeResult.deleted_counts``
    semantically means "would delete". ``tenant_anonymized`` is always
    False (no UPDATE issued). ``already_completed`` echoes the ticket's
    current status so callers can detect a re-run of a finished ticket.

    Q2 Option A locked per Cowork plan-review: separate function (not a
    ``dry_run: bool`` parameter on ``purge_tenant_data``) eliminates the
    accidentally-committed-in-dry-run failure class.

    Per CL-416: this function never deletes. Per CL-82: tenant_id filter
    applied per table even though service-role pool bypasses RLS — keeps
    the count contract identical to the real purge's WHERE clause.
    """
    would_delete: dict[str, int] = {}
    with get_pool().connection() as conn:
        tenant_id = _resolve_tenant_id_or_raise(conn, ticket_id)
        already_completed = _ticket_already_completed(conn, ticket_id)
        for table in _PURGE_ORDER:
            raw = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE tenant_id = %s",
                (str(tenant_id),),
            ).fetchone()
            row = cast("dict[str, Any]", raw)
            would_delete[table] = int(row["n"])
    return PurgeResult(
        ticket_id=ticket_id,
        tenant_id=tenant_id,
        deleted_counts=would_delete,
        tenant_anonymized=False,
        already_completed=already_completed,
    )


def _delete_where_tenant(conn: Any, table: str, tenant_id: UUID) -> int:
    """``DELETE FROM <table> WHERE tenant_id = %s``.

    ``table`` is hard-coded by the caller (selected from
    ``_PURGE_ORDER``); no user input reaches this function so the
    f-string composition is safe. ``tenant_id`` is bound as a parameter.
    Returns the rowcount the DELETE reported.
    """
    cur = conn.execute(
        f"DELETE FROM {table} WHERE tenant_id = %s",
        (str(tenant_id),),
    )
    return int(cur.rowcount)


def _anonymize_tenant_row(conn: Any, tenant_id: UUID) -> bool:
    """Anonymize the tenant row in place.

    Hard-deleting the row would break FK constraints from
    ``privacy_audit_log`` (DPDP-required retention) and ``dsr_tickets``
    (the completion tombstone). Anonymizing keeps those FKs valid and
    leaves no recoverable subject identity behind. Returns True when
    the UPDATE affected one row.
    """
    # Build the SET clause from _TENANT_ANONYMIZE so a newly-added identifying
    # column = one dict entry (no dict/UPDATE drift — the exact gap VT-160
    # closed). The keys are module-level constants (never user input), so
    # interpolating them as column names is injection-safe; the VALUES are
    # bound as parameters.
    columns = list(_TENANT_ANONYMIZE)
    set_clause = ", ".join(f"{col} = %s" for col in columns)
    params = [_TENANT_ANONYMIZE[col] for col in columns]
    params.append(str(tenant_id))
    cur = conn.execute(
        f"UPDATE tenants SET {set_clause} WHERE id = %s",  # noqa: S608 — keys are module constants
        tuple(params),
    )
    return int(cur.rowcount) == 1


def _mark_ticket_completed(conn: Any, ticket_id: UUID) -> None:
    """Set the ticket's status to ``'completed'`` + stamp
    ``completed_at``. The CHECK constraint at
    ``migrations/010_dsr_tickets.sql:8`` already permits
    ``'completed'`` — no migration needed."""
    conn.execute(
        "UPDATE dsr_tickets SET status = 'completed', completed_at = now() WHERE id = %s",
        (str(ticket_id),),
    )


__all__ = ["PurgeResult", "purge_tenant_data", "purge_tenant_data_dry_run"]


# ---------------------------------------------------------------------------
# VT-185 CLI entry point (Q3 Option A locked per Cowork plan-review).
#
# Usage:
#   python -m orchestrator.dsr_purge --ticket <uuid>            # real purge
#   python -m orchestrator.dsr_purge --ticket <uuid> --dry-run  # count only
#
# JSON output goes to stdout for ops-script consumption. Errors raise
# ValueError (missing ticket) or re-raise the underlying psycopg
# exception (FK constraint violation, etc.) — caller's responsibility
# to catch + interpret.
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="VT-185 DSR-purge CLI — purge tenant data on DSR ticket.",
    )
    parser.add_argument(
        "--ticket",
        required=True,
        type=UUID,
        help="dsr_tickets.id UUID (the deletion request)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Count rows that would be deleted across pipeline observability tables; commit nothing"
        ),
    )
    args = parser.parse_args()

    if args.dry_run:
        result = purge_tenant_data_dry_run(args.ticket)
    else:
        result = purge_tenant_data(args.ticket)

    print(
        json.dumps(
            {
                "ticket_id": str(result.ticket_id),
                "tenant_id": str(result.tenant_id),
                "deleted_counts": result.deleted_counts,
                "tenant_anonymized": result.tenant_anonymized,
                "already_completed": result.already_completed,
                "dry_run": args.dry_run,
            },
            indent=2,
        )
    )
    sys.exit(0)
