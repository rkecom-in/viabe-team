"""Tenant-scoped DB connection wrapper (CL-71 / CL-88 / CL-122).

Audit C1 (CL-71): ~12 tenant-scoped writers in orchestrator/ opened
``get_pool().connection()`` and wrote tenant tables without setting the
``app.current_tenant`` GUC. The suite passed only because CI runs as a
superuser, which bypasses ``FORCE ROW LEVEL SECURITY``; under any real role
every write would be rejected.

Architecture — option (C) of the CL-122 writeup
(https://gist.github.com/rkecom-in/2e60326f0f446cfd02da36d61246f18b):
a single privileged pool. ``tenant_connection`` downgrades the *effective*
role to ``app_role`` (non-superuser, no BYPASSRLS) for the duration of the
block via ``SET ROLE``, so RLS is real for tenant writers — while DBOS, the
LangGraph checkpointer and the service-role path keep the privileged role.

Scope is GUC-only (CL-88): no JWT / COALESCE policy variants.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from psycopg import Connection

from orchestrator.graph import get_pool


@contextmanager
def tenant_connection(
    tenant_id: UUID | str, *, pool: Any | None = None
) -> Iterator[Connection]:
    """Check out a pooled connection scoped to ``tenant_id`` for RLS enforcement.

    ``SET ROLE app_role`` drops the session's effective role to a non-superuser,
    no-BYPASSRLS role for the duration of the block; the ``app.current_tenant``
    GUC is then set so RLS policies resolve to this tenant. On exit both are
    reset so the pooled connection is safe for the next borrower.

    Every writer to a tenant-scoped table MUST enter through this. Direct
    ``get_pool().connection()`` is reserved for non-tenant tables and the
    intended service-role path (see migration 000b and ``orchestrator.db``).

    ``tenant_id`` accepts ``UUID`` or ``str``: the runner's ``@DBOS.step``
    functions carry it as ``str`` (DBOS serialises step arguments), handlers
    carry it as ``UUID``. Either way it is bound as text to ``set_config``.

    ``pool`` (VT-342) is injectable for tests — an explicit/mock pool; production
    (default ``None``) resolves the global privileged pool. The injected pool still
    goes through ``SET ROLE app_role`` + the GUC + reset, so a test exercises the
    real isolation path, not a bypass.
    """
    active_pool = pool if pool is not None else get_pool()
    with active_pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET ROLE app_role")
            cur.execute(
                "SELECT set_config('app.current_tenant', %s, false)",
                (str(tenant_id),),
            )
        try:
            yield conn
        finally:
            # Paired cleanup so the pooled connection carries no leaked state.
            # The pool's reset callback (graph.py) is the second line of
            # defence if this finally is bypassed.
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.current_tenant', '', false)")
                cur.execute("RESET ROLE")
