"""VT-77 — DSR self-serve EXPORT (the read variant of dsr_purge).

Gathers a tenant's data into a per-table JSON bundle + a ZIP, for a DPDP
data-access request. The DSR subject is the TENANT/owner (same as dsr_purge).

Privileged path — like dsr_purge, runs on the BYPASSRLS pool connection with
explicit ``WHERE tenant_id = %s`` scoping (some surfaces aren't in app_role's
grant; the export is a controller/admin action, not a tenant-client read).

PII posture (Cowork 20260603T154500Z, answer 1 — Phase-1 default (a)): export
SHAPE + TOKENS, never bulk-decrypted contact PII. A column denylist strips the
encrypted/raw-contact fields from every table before serialization. Decrypt-on-
explicit-legal-DSR-basis is a future extension (FLAGGED for Fazal/legal). The
manifest records the residency statement (VT-78) + the redaction posture.

Audit: dsr_export_requested (before) + dsr_export_completed (after) via the
VT-80 hash-chain (log_privacy_event).
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from orchestrator.graph import get_pool
from orchestrator.observability.audit_log import log_privacy_event

logger = logging.getLogger(__name__)

# Tenant-scoped tables exported (all carry tenant_id). l0_fragments is
# DELIBERATELY excluded: it is cross-tenant cohort-keyed aggregate with NO
# tenant_id (not tenant-identifiable data) — not part of a per-tenant export.
_EXPORT_TABLES: tuple[str, ...] = (
    "tenants",
    "tenant_oauth_tokens",
    "pipeline_runs",
    "pipeline_steps",
    "owner_inputs",
    "phone_token_resolutions",
    "customers",
)

# Phase-1 PII denylist (answer (a)): strip these columns from EVERY exported
# row — encrypted blobs (useless + sensitive) and raw contact PII. Belt-and-
# braces: any future sensitive column added to any table is excluded once named
# here, without changing the per-table SELECT (we SELECT * then pop).
_DENYLISTED_COLUMNS: frozenset[str] = frozenset(
    {
        "phone_number_encrypted",  # phone_token_resolutions (Fernet ciphertext)
        "refresh_token_encrypted",  # tenant_oauth_tokens
        "access_token",
        "refresh_token",
        "phone_e164",  # customers — raw contact PII (Phase-1: excluded)
    }
)

_REDACTION_NOTE = (
    "Phase-1 export redacts encrypted blobs and raw contact PII "
    "(phone_e164, *_encrypted, tokens). Decrypted/full-contact export is a "
    "future legal-DSR extension."
)


def _scrub(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in _DENYLISTED_COLUMNS}


def export_tenant_data(tenant_id: UUID | str, conn: Any = None) -> dict[str, Any]:
    """Gather the tenant's per-table data (PII-scrubbed). Returns a dict:
    ``{"tenant_id", "exported_at", "redaction_note", "tables": {name: [rows]}}``.

    Logs dsr_export_requested + dsr_export_completed on the VT-80 chain. ``conn``
    defaults to a fresh BYPASSRLS pool connection.
    """
    tid = str(tenant_id)
    if conn is None:
        with get_pool().connection() as own_conn:
            return export_tenant_data(tid, own_conn)

    log_privacy_event(
        conn,
        tenant_id=tid,
        event_type="dsr_export_requested",
        payload={"tables": list(_EXPORT_TABLES)},
        actor="dsr_export",
    )

    tables: dict[str, list[dict[str, Any]]] = {}
    for table in _EXPORT_TABLES:
        # The tenants row is keyed by ``id``; every other table by ``tenant_id``.
        key_col = "id" if table == "tenants" else "tenant_id"
        # Explicit tenant predicate (service-role bypasses RLS — mirror dsr_purge).
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {key_col} = %s",  # noqa: S608 — table + key from fixed allowlist
            (tid,),
        ).fetchall()
        scrubbed: list[dict[str, Any]] = []
        for r in rows:
            row = dict(r) if isinstance(r, dict) else _row_to_dict(conn, r)
            scrubbed.append(_scrub(row))
        tables[table] = scrubbed

    log_privacy_event(
        conn,
        tenant_id=tid,
        event_type="dsr_export_completed",
        payload={
            "tables": {t: len(rows) for t, rows in tables.items()},
        },
        actor="dsr_export",
    )

    return {
        "tenant_id": tid,
        "exported_at": datetime.now(UTC).isoformat(),
        "redaction_note": _REDACTION_NOTE,
        "tables": tables,
    }


def _row_to_dict(conn: Any, row: Any) -> dict[str, Any]:
    # Fallback for tuple rows: use the cursor description. The pool uses
    # dict_row so this is rarely hit; kept for connection-shape robustness.
    cols = [d.name for d in conn.cursor().description] if conn.cursor().description else []
    return dict(zip(cols, row, strict=False))


def build_export_zip(export: dict[str, Any]) -> bytes:
    """Render an export dict into a ZIP: manifest.json + <table>.json per table."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "tenant_id": export["tenant_id"],
            "exported_at": export["exported_at"],
            "redaction_note": export["redaction_note"],
            "data_residency": "Stored in India (ap-south-1). Processed per the DPDP Act.",
            "tables": {t: len(rows) for t, rows in export["tables"].items()},
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
        for table, rows in export["tables"].items():
            zf.writestr(f"{table}.json", json.dumps(rows, indent=2, default=str))
    return buf.getvalue()


__all__ = ["build_export_zip", "export_tenant_data"]
