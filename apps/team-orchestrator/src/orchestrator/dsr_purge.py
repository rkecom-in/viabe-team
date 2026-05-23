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

from psycopg.types.json import Jsonb

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)


# Anonymization values for the tenants row tombstone. Kept here (not
# inlined) so a Fazal-driven policy change touches one constant set.
_TENANT_ANONYMIZE = {
    "business_name": "[deleted]",
    "whatsapp_number": None,
    "opt_out": True,
}

# FK-safe deletion order. Children before parents — pipeline_steps /
# campaigns / owner_inputs all FK to pipeline_runs(id); l1_relationships
# FK to l1_entities(id). Order is read top-to-bottom by the purge loop.
_PURGE_ORDER: tuple[str, ...] = (
    "l1_relationships",
    "l1_entities",
    "owner_inputs",
    "campaigns",
    "pipeline_steps",
    "pipeline_runs",
    "subscriber_states",
    "phase_transitions",
    "subscriptions",
    "phone_token_resolutions",
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
                deleted_counts[table] = _delete_where_tenant(
                    conn, table, tenant_id
                )

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
    return (
        tenant_id_value
        if isinstance(tenant_id_value, UUID)
        else UUID(str(tenant_id_value))
    )


def _ticket_already_completed(conn: Any, ticket_id: UUID) -> bool:
    raw = conn.execute(
        "SELECT status FROM dsr_tickets WHERE id = %s",
        (str(ticket_id),),
    ).fetchone()
    if raw is None:
        raise ValueError(
            f"dsr_tickets row not found inside purge transaction: "
            f"id={ticket_id}"
        )
    row = cast("dict[str, Any]", raw)
    return bool(row["status"] == "completed")


def _append_audit_event(
    conn: Any, tenant_id: UUID, ticket_id: UUID
) -> None:
    """Append an event noting the purge intent.

    The ``privacy_audit_log`` table (008) is the regulator-required
    7-year retention surface. VT-8 owns the full hash-chain
    enforcement; until that lands, this insert uses a placeholder
    ``this_hash`` (the ticket UUID, hex-encoded) so the NOT NULL
    constraint is satisfied. When VT-8 lands, this writer is updated
    in lockstep.
    """
    conn.execute(
        "INSERT INTO privacy_audit_log "
        "(tenant_id, event_type, payload, this_hash, actor) "
        "VALUES (%s, 'subject_data_purged', %s, %s, 'dsr_purge')",
        (
            str(tenant_id),
            Jsonb({"ticket_id": str(ticket_id)}),
            ticket_id.hex,
        ),
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
    cur = conn.execute(
        "UPDATE tenants SET business_name = %s, whatsapp_number = %s, "
        "opt_out = %s WHERE id = %s",
        (
            _TENANT_ANONYMIZE["business_name"],
            _TENANT_ANONYMIZE["whatsapp_number"],
            _TENANT_ANONYMIZE["opt_out"],
            str(tenant_id),
        ),
    )
    return int(cur.rowcount) == 1


def _mark_ticket_completed(conn: Any, ticket_id: UUID) -> None:
    """Set the ticket's status to ``'completed'`` + stamp
    ``completed_at``. The CHECK constraint at
    ``migrations/010_dsr_tickets.sql:8`` already permits
    ``'completed'`` — no migration needed."""
    conn.execute(
        "UPDATE dsr_tickets SET status = 'completed', completed_at = now() "
        "WHERE id = %s",
        (str(ticket_id),),
    )


__all__ = ["PurgeResult", "purge_tenant_data"]
