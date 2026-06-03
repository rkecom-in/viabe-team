"""VT-72 — typed tenant-scoped table wrappers (Phase-1 hot tables).

One thin wrapper per LIVE tenant-scoped hot table. Each inherits the tenant
predicate + result validation from ``TenantScopedTable``. NEW code should read/
write these tables through these wrappers (the `no-direct-tenant-db-access` lint
gates regressions); existing sites are allowlisted pending the VT-306 migration.

Unbuilt-table wrappers (EpisodicEvents / KGEventsProcessed / CompositionAudits /
l3_patterns / l4_documents) are DEFERRED to VT-306 — wrapping unbuilt tables is
exactly the stale thing the no-stale bar forbids.
"""

from __future__ import annotations

from orchestrator.db.base import TenantScopedTable


class CustomersWrapper(TenantScopedTable):
    _table = "customers"


class CampaignsWrapper(TenantScopedTable):
    _table = "campaigns"


class PendingApprovalsWrapper(TenantScopedTable):
    _table = "pending_approvals"


class OwnerInputsWrapper(TenantScopedTable):
    _table = "owner_inputs"


class PhoneTokenResolutionsWrapper(TenantScopedTable):
    _table = "phone_token_resolutions"
    _id_col = "phone_token"  # PK is the token, not `id`


__all__ = [
    "CampaignsWrapper",
    "CustomersWrapper",
    "OwnerInputsWrapper",
    "PendingApprovalsWrapper",
    "PhoneTokenResolutionsWrapper",
]
