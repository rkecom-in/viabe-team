# Tenant-scoped vs cross-tenant tables (VT-72)

Isolation layer-2 (typed wrappers) policy. Layer-1 is RLS (`tenant_connection`);
layer-3 is agent context isolation (VT-73).

## Rule: `no-direct-tenant-db-access`
NEW code MUST access tenant-scoped hot tables through
`orchestrator.db.wrappers` (subclasses of `TenantScopedTable`), never via raw
SQL. Enforced by `scripts/check_no_direct_tenant_db_access.py` (CI gate
`gate-no-direct-tenant-db-access`) in **report/allowlist mode**: existing
direct-access sites are allowlisted (layer-1 RLS protects them); a NEW
non-allowlisted file touching these tables fails the gate.

## Tenant-scoped hot tables — WRAPPED (Phase-1, VT-72)
| Table | Wrapper |
|-------|---------|
| customers | `CustomersWrapper` |
| campaigns | `CampaignsWrapper` |
| pending_approvals | `PendingApprovalsWrapper` |
| owner_inputs | `OwnerInputsWrapper` |
| phone_token_resolutions | `PhoneTokenResolutionsWrapper` |

Each enforces: `tenant_id`-first methods · mandatory `WHERE tenant_id = %s` ·
post-fetch `assert_tenant_scoped` (mismatch → `TenantIsolationError` + a
`tenant_isolation_breach` step → VT-79 Detector-1 P0).

## Cross-tenant / reference tables — NO wrapper (direct access OK)
These are global reference / aggregate data with NO `tenant_id` (or
cohort-keyed), so they are NOT in the lint's table set:
- `l0_fragments` (cohort-keyed aggregate; k-anonymity, no tenant_id)
- `l3_patterns`, `l4_documents` (unbuilt; cross-tenant aggregate/corpus)
- `localities`, `business_types`, `platforms` (reference data)
- `privacy_audit_log` (global chain, service-role-only by grant-exclusion — VT-80)

## Deferred (VT-306)
- Wrappers for unbuilt tenant tables (EpisodicEvents/VT-66, KGEventsProcessed/VT-65,
  CompositionAudits/VT-71) — wrapping unbuilt tables is stale; add with the substrate.
- Full call-site migration of the ~22 allowlisted existing direct-access sites.
- Flip the lint to hard-fail (empty allowlist) once migration completes.
