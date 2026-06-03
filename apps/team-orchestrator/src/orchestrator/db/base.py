"""VT-72 — typed tenant-scoped table wrappers (isolation layer-2).

``TenantScopedTable`` is the load-bearing primitive: every public method takes
``tenant_id`` first, every generated query carries a mandatory
``WHERE tenant_id = %s`` predicate, and every returned row is validated via
``_tenant_guard.assert_tenant_scoped`` (Pillar 8 — ONE validation path). A row
whose tenant_id != the input raises ``TenantIsolationError`` AND emits a
``tenant_isolation_breach`` step (→ VT-79 Detector-1 P0).

This is layer-2 of the three independent isolation layers (Pillar 3): RLS
(layer-1, ``tenant_connection``) + typed wrappers (here) + agent context
isolation (layer-3, VT-73). Each connection is acquired through
``tenant_connection`` so RLS is ALSO enforced — defence in depth.

Phase-1 cut (Cowork 20260603T163500Z): wrappers for the LIVE hot tables only.
Unbuilt-table wrappers + the full call-site migration + the hard-fail lint are
VT-306 (rostered). The Phase-1 lint (`scripts/check_no_direct_tenant_db_access.py`)
gates NEW direct access in report/allowlist mode.
"""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import UUID

from orchestrator._tenant_guard import assert_tenant_scoped
from orchestrator.db import tenant_connection


class TenantScopedTable:
    """Base for a typed, tenant-scoped table accessor.

    Subclasses set ``_table`` (and ``_id_col`` if the PK is not ``id``). All
    queries are tenant-predicated + result-validated.
    """

    _table: ClassVar[str]
    _id_col: ClassVar[str] = "id"

    def _uuid(self, tenant_id: UUID | str) -> UUID:
        return tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))

    def _validate(self, rows: list[dict[str, Any]], tenant_id: UUID) -> None:
        # belt-and-braces over RLS — raises TenantIsolationError + emits the
        # tenant_isolation_breach step (VT-79 Detector-1) on any mismatch.
        assert_tenant_scoped(rows, tenant_id)

    def list_for_tenant(
        self, tenant_id: UUID | str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        tid = self._uuid(tenant_id)
        with tenant_connection(tid) as conn:
            rows = conn.execute(
                f"SELECT * FROM {self._table} WHERE tenant_id = %s LIMIT %s",  # noqa: S608 — _table is a fixed class attr, never user input
                (str(tid), limit),
            ).fetchall()
        out = [dict(r) for r in rows]
        self._validate(out, tid)
        return out

    def find_by_id(
        self, tenant_id: UUID | str, row_id: UUID | str
    ) -> dict[str, Any] | None:
        tid = self._uuid(tenant_id)
        with tenant_connection(tid) as conn:
            row = conn.execute(
                f"SELECT * FROM {self._table} "  # noqa: S608 — fixed class attrs
                f"WHERE tenant_id = %s AND {self._id_col} = %s",
                (str(tid), str(row_id)),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        self._validate([d], tid)
        return d

    def insert(
        self, tenant_id: UUID | str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Insert a row with tenant_id FORCED to the scoped tenant (a payload
        tenant_id is ignored — the wrapper owns scoping)."""
        tid = self._uuid(tenant_id)
        data = {k: v for k, v in payload.items() if k != "tenant_id"}
        data["tenant_id"] = str(tid)
        cols = list(data.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        collist = ", ".join(cols)
        with tenant_connection(tid) as conn:
            row = conn.execute(
                f"INSERT INTO {self._table} ({collist}) "  # noqa: S608 — cols from caller keys, values parameterised
                f"VALUES ({placeholders}) RETURNING *",
                tuple(data[c] for c in cols),
            ).fetchone()
        d = dict(row) if row is not None else {}
        self._validate([d] if d else [], tid)
        return d

    def delete(self, tenant_id: UUID | str, row_id: UUID | str) -> int:
        tid = self._uuid(tenant_id)
        with tenant_connection(tid) as conn:
            cur = conn.execute(
                f"DELETE FROM {self._table} "  # noqa: S608 — fixed class attrs
                f"WHERE tenant_id = %s AND {self._id_col} = %s",
                (str(tid), str(row_id)),
            )
            return cur.rowcount


__all__ = ["TenantScopedTable"]
