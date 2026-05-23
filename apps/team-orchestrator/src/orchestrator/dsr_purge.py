"""DSR deletion fulfillment — tenant-wide subject data purge (VT-DSR-Purge).

The DPDP / privacy regime promises Right-to-Erasure: a subject can
request deletion and the controller MUST honor it. Until this module
landed, ``dsr_handler.py`` only acknowledged tickets — no code path set
``dsr_tickets.status = 'completed'`` because no code actually deleted
anything. This module closes that gap for **tenant-wide** subject
requests (the tenant IS the DSR subject for the Viabe-Team product;
end-customer data lives inside the tenant's records and goes with it).

Architecture
------------
- Single public entry point: ``purge_tenant_data(ticket_id)``. Takes a
  ``dsr_tickets.id``, reads the ticket's ``tenant_id``, and runs the
  purge in a sequence that respects FK ordering.
- Per-table delete functions live in this module so the inventory + the
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
  DELETE, so the audit chain captures the request.
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

from orchestrator.db import tenant_connection

logger = logging.getLogger(__name__)


# Anonymization values for the tenants row tombstone. Kept here (not
# inlined) so a Fazal-driven policy change touches one constant set.
_TENANT_ANONYMIZE = {
    "business_name": "[deleted]",
    "whatsapp_number": None,
    "opt_out": True,
}


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
      4. DELETE child tables in FK-safe order, all under
         ``tenant_connection(tenant_id)`` so RLS + GUC are scoped.
      5. UPDATE ``tenants`` row → anonymized tombstone (kept for FK
         integrity with privacy_audit_log + dsr_tickets).
      6. UPDATE the ticket → status='completed', completed_at=now().

    The whole purge runs inside a single ``conn.transaction()`` so a
    failure mid-sequence rolls back to a coherent state. Re-running
    the ticket then re-runs the full sequence.
    """
    tenant_id = _resolve_tenant_id_or_raise(ticket_id)
    deleted_counts: dict[str, int] = {}
    tenant_anonymized = False

    with tenant_connection(tenant_id) as conn, conn.transaction():
        if _ticket_already_completed(conn, ticket_id):
            return PurgeResult(
                ticket_id=ticket_id,
                tenant_id=tenant_id,
                deleted_counts={},
                tenant_anonymized=False,
                already_completed=True,
            )

        _append_audit_event(conn, tenant_id, ticket_id)

        # FK-safe deletion order — children before parents. Each
        # DELETE returns rowcount; counts go into the result for
        # test + telemetry.
        deleted_counts["l1_relationships"] = _delete_where_tenant(
            conn, "l1_relationships"
        )
        deleted_counts["l1_entities"] = _delete_where_tenant(
            conn, "l1_entities"
        )
        deleted_counts["owner_inputs"] = _delete_where_tenant(
            conn, "owner_inputs"
        )
        deleted_counts["campaigns"] = _delete_where_tenant(
            conn, "campaigns"
        )
        # pipeline_steps before pipeline_runs (FK).
        deleted_counts["pipeline_steps"] = _delete_where_tenant(
            conn, "pipeline_steps"
        )
        deleted_counts["pipeline_runs"] = _delete_where_tenant(
            conn, "pipeline_runs"
        )
        deleted_counts["subscriber_states"] = _delete_where_tenant(
            conn, "subscriber_states"
        )
        deleted_counts["phase_transitions"] = _delete_where_tenant(
            conn, "phase_transitions"
        )
        deleted_counts["subscriptions"] = _delete_where_tenant(
            conn, "subscriptions"
        )
        deleted_counts["phone_token_resolutions"] = _delete_where_tenant(
            conn, "phone_token_resolutions"
        )
        deleted_counts["twilio_inbound_events"] = _delete_where_tenant(
            conn, "twilio_inbound_events"
        )
        deleted_counts["rate_limit_buckets"] = _delete_where_tenant(
            conn, "rate_limit_buckets"
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


def _resolve_tenant_id_or_raise(ticket_id: UUID) -> UUID:
    """Read the ticket's ``tenant_id`` via a superuser connection.

    Reading the ticket needs to happen BEFORE we enter
    ``tenant_connection(tenant_id)`` — chicken-and-egg. Use the
    pool's bare connection (RLS-bypassing service role) for this
    one lookup. The actual purge runs under the tenant-scoped
    connection.
    """
    from orchestrator.graph import get_pool

    with get_pool().connection() as conn:
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
            f"dsr_tickets row not found inside tenant_connection: "
            f"id={ticket_id} (RLS mismatch?)"
        )
    row = cast("dict[str, Any]", raw)
    return bool(row["status"] == "completed")


def _append_audit_event(
    conn: Any, tenant_id: UUID, ticket_id: UUID
) -> None:
    """Append a hash-chain-style event noting the purge intent.

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


def _delete_where_tenant(conn: Any, table: str) -> int:
    """DELETE ... WHERE tenant_id = app_current_tenant().

    The ``tenant_connection`` already sets ``app.current_tenant`` so
    RLS filters to this tenant; the explicit predicate is belt-and-
    braces. ``table`` is hard-coded by the caller (no user-supplied
    value reaches this function) so the SQL composition is safe.
    Returns the rowcount the DELETE reported.
    """
    cur = conn.execute(
        f"DELETE FROM {table} WHERE tenant_id = app_current_tenant()"
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
