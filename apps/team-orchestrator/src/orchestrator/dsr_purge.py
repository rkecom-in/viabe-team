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
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from orchestrator.graph import get_pool
from orchestrator.observability.audit_log import log_privacy_event

logger = logging.getLogger(__name__)


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
    # VT-361 (mig 120): the verification result carries the authoritative business/owner name + the
    # GSTIN (a strong re-id anchor) — both MUST be scrubbed on DSR-delete. verification_status /
    # method / verified_at are non-PII operational flags, left intact.
    "verified_business_name": None,
    "gstin": None,
}

# FK-safe deletion order. Children before parents — pipeline_steps /
# campaigns / owner_inputs all FK to pipeline_runs(id); l1_relationships
# FK to l1_entities(id). Order is read top-to-bottom by the purge loop.
_PURGE_ORDER: tuple[str, ...] = (
    "l1_relationships",
    "l1_entities",
    # VT-366: the Auto-Discovery DRAFT business profile (tenant business data — public-sourced but
    # still tenant-scoped). Leaf table (FK to tenants only), no CASCADE, no other hard-delete path,
    # so a tenant DSR-delete MUST sweep it here or the draft survives the purge.
    "business_profile_draft",
    # VT-367: the onboarding-journey cursor (holds owner-supplied business answers). Leaf (FK tenants
    # only), no CASCADE — sweep on DSR or the journey answers survive the purge (the 2a lesson).
    "onboarding_journey",
    # VT-368: the business-plan spine (summary + roadmap + frozen fact bundle = owner business
    # context, EVERY version). Leaf (FK tenants only), no CASCADE — hard-delete all versions on DSR.
    "business_plan",
    # VT-369: the agent surface. agent_drafts (customer-facing message params) → batches → work
    # items, children-first; agent_customer_contacts is the per-customer contact ledger (customer
    # linkage at rest). All FK tenants with CASCADE, but the tenant row is anonymized NOT deleted on
    # DSR (the CASCADE never fires) — sweep explicitly or they survive the purge (the VT-366 lesson).
    # VT-382 (CL-437.3, mig 135): owner_message_audit holds the EXACT owner-facing sent text — the
    # ONE surface that retains it after the outbox redaction. Leaf (FK tenants only; CASCADE never
    # fires), but it carries draft/batch linkage by value, so it sweeps FIRST among the agent
    # tables (children-first hygiene). MUST be swept here or the exact text survives the purge.
    "owner_message_audit",
    "agent_drafts",
    "agent_draft_batches",
    "agent_work_items",
    "agent_customer_contacts",
    # VT-369 PR-2: the per-(tenant, agent) autonomy state (trust counters + grant evidence link).
    # Leaf (FK tenants only; CASCADE never fires — the tenant row is anonymized, not deleted).
    "tenant_agent_autonomy",
    # VT-467: the per-(tenant, action_class) business-impact autonomy state (tier + thresholds +
    # grant provenance). Leaf (FK tenants only; CASCADE never fires — tenant anonymized, not deleted),
    # so a tenant DSR-delete MUST sweep it here or the business-autonomy grants survive the purge
    # (the tenant_agent_autonomy lesson, on the new table).
    "tenant_business_autonomy",
    # VT-474: the per-tenant business POLICY (allowed action-types / segments / freq-caps / spend
    # ceiling — the A2 machine-enforceable bound). Leaf (FK tenants only; CASCADE never fires — tenant
    # anonymized, not deleted), so a tenant DSR-delete MUST sweep it here or the policy grant survives
    # the purge (the tenant_business_autonomy lesson, on the new single-row-per-tenant table).
    "tenant_business_policy",
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
    # VT-327: KG transactional outbox + its consumer ledger. Leaf (both FK tenants only;
    # kg_events_processed.event_id is a PK, NOT a FK to kg_events → order-insensitive between
    # them). kg_events.payload carries the TENANT_CREATED business_name (owner PII) at rest,
    # and the drain only stamps drained_at (never deletes) — so a tenant DSR MUST sweep both
    # here or the outbox PII survives the purge (the episodic_events/platform_listings lesson).
    "kg_events_processed",
    "kg_events",
    "kyc_verification_log",  # VT-361 (mig 120): per-tenant verification attempts — leaf (FK tenants
    # only; anonymize never CASCADEs). Result-only (no payer names), but it's the subject's
    # verification history → hard-delete on DSR (the episodic_events/platform_listings lesson).
    "tenant_mca_data",  # VT-449/VT-411 (mig 142): the parsed MCA company-master at rest. Leaf (no FK
    # in/out — tenant_id-scoped, the tenant row is anonymized NOT deleted on DSR, so no CASCADE path).
    # registered_address_encrypted + directors_encrypted hold ENCRYPTED PII (director names + address)
    # — MUST be swept here or the subject's encrypted ownership PII survives erasure (the
    # tenant_oauth_tokens credential lesson — encrypted-at-rest is still subject data). Hard-delete.
    "founding_tier_claims",  # VT-94: per-tenant founding claim (audit) — hard-delete on DSR
    "template_error_reports",  # VT-335: owner template-error reports (PII free text) — hard-delete
    "owner_inputs",
    "campaigns",
    # VT-374 (mig 131): the run-control substrate. step_overrides carries redacted-at-write
    # pinned_input/pinned_output/reason (still the tenant's control history — a PII-at-rest
    # surface, F7); workflow_controls carries pause records + redacted reasons. Both leaf
    # (FK tenants only, no CASCADE — the tenant row is anonymized, never deleted, so a sweep
    # here is the ONLY hard-delete path). workflow_id/consumed_run_id point at pipeline_runs
    # ids WITHOUT an FK, but they're swept BEFORE the pipeline tables anyway so control rows
    # never outlive the runs they controlled.
    "step_overrides",
    "workflow_controls",
    # VT-518 (DSR-purge gap, Cowork audit-after of VT-514/515): the two tenant-scoped
    # PII-bearing observability tables added by VT-514/VT-515 were NEVER in this order →
    # a right-to-erasure tenant survived in the audit + debug logs (redact-at-write +
    # RLS is insufficient for erasure — redacted activity history is STILL the subject's
    # data). Both swept here, BEFORE pipeline_runs:
    #   * tm_audit_log (mig 147): run_id → pipeline_runs(id) NO ACTION, so it MUST precede
    #     pipeline_runs or the runs delete fails on the dangling ref. parent_audit_id is a
    #     self-FK (→ tm_audit_log.id) NO ACTION — a single `DELETE … WHERE tenant_id = %s`
    #     removes parent+child in ONE statement, so the FK is checked at statement end with
    #     both already gone (safe; no per-row RESTRICT). tenant_id NOT NULL.
    #   * debug_events (mig 146): no FK at all (trace_id is bare TEXT), order-insensitive.
    #     tenant_id is NULLABLE (pre-tenant failures carry no tenant) — the tenant-scoped
    #     DELETE correctly sweeps only the subject's rows and leaves NULL-tenant rows (not
    #     subject data). mig 147 documented "DSR-purge-scoped via the VT-185 path"; this row
    #     is the impl that finally delivers it (the recurring hand-maintained-order drift).
    "tm_audit_log",
    "debug_events",
    # VT-524 (B1): owner_notifications — the owner-notification delivery ledger. Tenant-scoped
    # (tenant_id → tenants, FK; run_id is a soft NO-FK pointer per VT-521), so order-insensitive.
    # Holds message_sid/template_name/status — tenant data → erased on right-to-erasure.
    "owner_notifications",
    # VT-525 (B2): the manager task/step spine. Both tenant-scoped (tenant_id → tenants). Steps
    # FK task_id → manager_tasks ON DELETE CASCADE; listed steps-BEFORE-tasks (children first,
    # house convention) though the CASCADE would also cover it. evidence_ref is a by-value
    # pointer (NO FK), so order re: pipeline_runs is insensitive. Redacted objective/step detail
    # is STILL the subject's data → erased on right-to-erasure (the VT-518 lesson).
    "manager_task_steps",
    "manager_tasks",
    # VT-527 (B4): pending_questions — the owner-clarification ledger. Tenant-scoped; task_id/run_id
    # are soft pointers (NO FK), so order-insensitive. Holds redacted question + the owner's redacted
    # answer → tenant data, erased on right-to-erasure.
    "pending_questions",
    # VT-531 (C3): agent_corrections — the reviewer-correction store. Tenant-scoped; run_id/batch_id
    # are soft pointers (NO FK), so order-insensitive. Holds the PII-redacted correction text →
    # the subject's data, erased on right-to-erasure.
    "agent_corrections",
    # VT-550 (C3b): agent_memory — the seedable learnable-memory store. Only TENANT rows are the
    # subject's data; the WHERE tenant_id=… purge erases those and naturally skips GLOBAL seeds
    # (tenant_id IS NULL — archetype knowledge, not a subject's data).
    "agent_memory",
    # VT-552 (B1 part-2b): incidents — durable incident records (run_id soft, no FK), tenant data,
    # erased on right-to-erasure.
    "incidents",
    # VT-579: conversation_log — the LIFETIME owner↔system conversation (both directions). Leaf (FK
    # tenants ON DELETE CASCADE, but the tenant row is anonymized NOT deleted on DSR, so the CASCADE
    # never fires) → order-insensitive; MUST be swept here or the subject's own conversation survives
    # erasure (the episodic_events/agent_memory lesson). Retention = lifetime-of-relationship, DSR-only
    # deletion (CL-2026-07-03-conversation-memory-architecture). Hard-delete.
    "conversation_log",
    "pipeline_steps",
    "pipeline_runs",
    "subscriber_states",
    "phase_transitions",
    "subscriptions",
    "phone_token_resolutions",
    # VT-422 (GAP-1, DPDP erasure bug): the per-(tenant, connector) ENCRYPTED OAuth
    # credential (the Shopify offline access token et al — the tenant's credential =
    # tenant PII per CL-390/425). Leaf (FK tenants only; the tenant row is anonymized,
    # NOT deleted on DSR, so the FK CASCADE never fires) → order-insensitive. It was
    # EXPORTED on DSR (dsr_export.py) but never ERASED — a real privacy-at-rest bug: a
    # DSR-deleted tenant's encrypted token survived the purge. MUST be swept here or
    # the credential outlives erasure (the episodic_events/platform_listings lesson,
    # on the credential store). Hard-delete.
    "tenant_oauth_tokens",
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
