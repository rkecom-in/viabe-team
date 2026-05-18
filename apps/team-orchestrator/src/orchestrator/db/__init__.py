"""Database access for the orchestrator.

Tenant-isolation rule (CL-71 / CL-122)
--------------------------------------
Every writer (or reader) of a tenant-scoped table MUST acquire its connection
through ``tenant_connection(tenant_id)``. That wrapper does ``SET ROLE
app_role`` and sets the ``app.current_tenant`` GUC, so ``FORCE ROW LEVEL
SECURITY`` is genuinely enforced for the block.

Direct ``get_pool().connection()`` is reserved for:
  (a) the explicit service-role path of migration 000b — ``_lookup_tenant``
      (resolves the tenant) and ``_within_rate_limits`` (writes the
      cross-tenant workspace-sentinel bucket);
  (b) DBOS framework operations (DBOS owns and migrates its own schema);
  (c) the LangGraph PostgresSaver checkpointer.
"""

from orchestrator.db.tenant_connection import tenant_connection

__all__ = ["tenant_connection"]
