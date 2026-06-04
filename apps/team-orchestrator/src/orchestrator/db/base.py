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

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar
from uuid import UUID

from orchestrator._tenant_guard import TenantIsolationError, assert_tenant_scoped
from orchestrator.db import tenant_connection


class TenantScopedTable:
    """Base for a typed, tenant-scoped table accessor.

    Subclasses set ``_table`` (and ``_id_col`` if the PK is not ``id``). All
    queries are tenant-predicated + result-validated.

    Every method takes an optional ``conn`` (VT-306): when given, the query runs
    on that caller-owned connection (so a write can be ATOMIC with a sibling
    write — e.g. the VT-65 PR-2 ``customers`` UPDATE + ``customer_updated``
    kg_emit in one txn); when ``None``, a fresh ``tenant_connection`` is opened.
    The caller's conn MUST already be tenant-scoped (its own tenant_connection).
    """

    _table: ClassVar[str]
    _id_col: ClassVar[str] = "id"

    def _uuid(self, tenant_id: UUID | str) -> UUID:
        return tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))

    @contextmanager
    def _conn(self, tid: UUID, conn: Any) -> Iterator[Any]:
        """Yield the caller's ``conn`` if given (atomic composition), else open a
        fresh tenant_connection for ``tid`` (RLS + GUC).

        VT-306 (defense-in-depth): a caller-supplied ``conn`` MUST be a
        tenant_connection (``SET ROLE app_role`` + GUC). If it isn't — e.g. a raw
        ``get_pool()`` / ``pool.connection()`` + ``set_config`` cursor on a
        BYPASSRLS pool role — layer-1 RLS is INERT and isolation would rest only
        on the WHERE clause. We REJECT such a conn so that contract can't be
        violated silently (the `conn=` path is load-bearing)."""
        if conn is not None:
            self._assert_app_role(conn)
            yield conn
        else:
            with tenant_connection(tid) as own:
                yield own

    def _assert_app_role(self, conn: Any) -> None:
        """Raise TenantIsolationError if ``conn`` is not running as ``app_role``.
        Mock/closed conns (whose current_user isn't a real str) are skipped — the
        guard targets real BYPASSRLS pool conns fed via ``conn=``."""
        try:
            row = conn.execute("SELECT current_user AS u").fetchone()
        except Exception:  # noqa: BLE001 — can't introspect (mock/closed) → skip
            return
        user = (row.get("u") if isinstance(row, dict) else (row[0] if row else None))
        if isinstance(user, str) and user != "app_role":
            raise TenantIsolationError(
                f"wrapper conn= must be a tenant_connection (app_role); got role "
                f"{user!r} — a non-app_role conn defeats layer-1 RLS (VT-306)."
            )

    def _validate(self, rows: list[dict[str, Any]], tenant_id: UUID) -> None:
        # belt-and-braces over RLS — raises TenantIsolationError + emits the
        # tenant_isolation_breach step (VT-79 Detector-1) on any mismatch.
        assert_tenant_scoped(rows, tenant_id)

    def list_for_tenant(
        self, tenant_id: UUID | str, *, limit: int = 200, conn: Any = None
    ) -> list[dict[str, Any]]:
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            rows = c.execute(
                f"SELECT * FROM {self._table} WHERE tenant_id = %s LIMIT %s",  # noqa: S608 — _table is a fixed class attr, never user input
                (str(tid), limit),
            ).fetchall()
        out = [dict(r) for r in rows]
        self._validate(out, tid)
        return out

    def find_by_id(
        self, tenant_id: UUID | str, row_id: UUID | str, *, conn: Any = None
    ) -> dict[str, Any] | None:
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            row = c.execute(
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
        self, tenant_id: UUID | str, payload: dict[str, Any], *, conn: Any = None
    ) -> dict[str, Any]:
        """Insert a row with tenant_id FORCED to the scoped tenant (a payload
        tenant_id is ignored — the wrapper owns scoping)."""
        tid = self._uuid(tenant_id)
        data = {k: v for k, v in payload.items() if k != "tenant_id"}
        data["tenant_id"] = str(tid)
        cols = list(data.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        collist = ", ".join(cols)
        with self._conn(tid, conn) as c:
            row = c.execute(
                f"INSERT INTO {self._table} ({collist}) "  # noqa: S608 — cols from caller keys, values parameterised
                f"VALUES ({placeholders}) RETURNING *",
                tuple(data[c] for c in cols),
            ).fetchone()
        d = dict(row) if row is not None else {}
        self._validate([d] if d else [], tid)
        return d

    def delete(
        self, tenant_id: UUID | str, row_id: UUID | str, *, conn: Any = None
    ) -> int:
        tid = self._uuid(tenant_id)
        with self._conn(tid, conn) as c:
            cur = c.execute(
                f"DELETE FROM {self._table} "  # noqa: S608 — fixed class attrs
                f"WHERE tenant_id = %s AND {self._id_col} = %s",
                (str(tid), str(row_id)),
            )
            return cur.rowcount


__all__ = ["TenantScopedTable"]
