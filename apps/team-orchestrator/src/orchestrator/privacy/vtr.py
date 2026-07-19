"""VT-281 / VT-377 — VTR (Viabe Team Representative) de-identified, assignment-scoped access.

Fork A (Cowork plan-ack 20260606T231500Z): the VTR sees customer data ONLY through the
de-identified views (`vtr_customers`, `vtr_escalations`, migration 115, and the later vtr_* views),
entered as `app_vtr_role` — a role with NO grant on the raw PII tables (customers,
phone_token_resolutions) or the decrypt function, so PII is UNREACHABLE from the VTR even via an
app bug (not merely masked app-side).

VT-377 (migration 134) closes the multi-VTR precondition: every app_vtr_role view is now
ASSIGNMENT-SCOPED — `WHERE current_user = 'app_vtr_admin_role' OR tenant_id IN (SELECT tenant_id
FROM operator_assignments WHERE operator_id = app_vtr_operator() AND unassigned_at IS NULL)`.
The operator identity reaches the view via the `app.vtr_operator_id` GUC, set ONLY here from the
VERIFIED operator id (post JWT-verify / `require_vtr_action` — never a raw client field). No
operator (or an empty GUC) ⇒ the scoped subquery matches nothing — fail-closed. The assignment
substrate is the EXISTING `operator_assignments` table (mig-072) — the stale `vtr_assignments`
docstring references across migs 115/118/130/131/132 are RETIRED by mig-134.

This module provides:
- :func:`bootstrap_vtr_ref_secret` — seed the REF# keying secret from env VT_REF_HMAC_KEY (the
  views compute `REF# = HMAC(customer_id, secret)` via the view owner's rights; the secret is never
  granted to app_vtr_role). Env, never client.
- :func:`vtr_connection` — a pooled connection with `SET ROLE app_vtr_role` for the duration (the
  VT-280 digest + the canary read the views through this). Mirrors `tenant_connection`'s SET ROLE.
  VT-377: pass ``operator_id`` to scope the mig-134 views to that operator's active assignments.
- :func:`vtr_admin_connection` — `SET ROLE app_vtr_admin_role` (the audited exception tier,
  Fazal=VTR#1): the mig-134 predicate's role leg keeps all-tenants — role IS the mechanism, no
  bypass flags (Cowork ruling 20260612T011000Z).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from orchestrator.graph import get_pool

_REF_KEY_ENV = "VT_REF_HMAC_KEY"
# The mig-134 scoping GUC. Set txn-local from the VERIFIED operator id only — never client input.
_VTR_OPERATOR_GUC = "app.vtr_operator_id"


def _fazal_owner_uuid() -> str:
    """The FAZAL_OWNER_UUID env (the VTAdmin break-glass identity — the same comparison
    `ops_common.operator_assigned` / `require_exception_tier` use). Empty when unset."""
    return (os.environ.get("FAZAL_OWNER_UUID", "") or "").strip()


def bootstrap_vtr_ref_secret(key: str | None = None, *, pool: Any | None = None) -> bool:
    """Idempotently seed `vtr_ref_secret` (the singleton REF# keying secret) from env (or ``key``).

    Returns True if a secret is present after the call. Service-role (the table is deny-all RLS;
    app_vtr_role can't reach it). The orchestrator calls this at startup; the env value is the
    source of truth (env, never client). Raises if no key is available — a missing key would make
    the views emit NULL refs, which must fail loud, not silently de-correlate the VTR view."""
    secret = key or os.environ.get(_REF_KEY_ENV)
    if not secret:
        raise RuntimeError(
            f"{_REF_KEY_ENV} is not set — the VTR REF# keying secret is required (VT-281)"
        )
    active_pool = pool if pool is not None else get_pool()
    with active_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO vtr_ref_secret (id, secret) VALUES (true, %s) "
            "ON CONFLICT (id) DO UPDATE SET secret = EXCLUDED.secret",
            (secret,),
        )
    return True


@contextmanager
def vtr_connection(*, operator_id: str | None = None, pool: Any | None = None) -> Iterator[Any]:
    """Check out a pooled connection with `SET ROLE app_vtr_role` for the duration.

    The VTR path (VT-280 digest, the Gap-6/run-control reads, the canary) reads ONLY the
    de-identified views through this — a raw-PII read raises permission-denied because
    app_vtr_role has no grant on the base tables. `RESET ROLE` on exit so the pooled connection
    carries no leaked state.

    VT-377 scoping (mig-134): pass the VERIFIED operator id (the `require_vtr_action` /
    JWT-claim return value — NEVER a raw body field) and it is set as the txn-local
    `app.vtr_operator_id` GUC, scoping every vtr_* view to that operator's ACTIVE
    operator_assignments. The GUC is txn-local inside an explicit transaction (the service pool
    is autocommit — a bare txn-local set_config would evaporate at its own statement end), so it
    structurally cannot outlive this checkout (the ops_resolve set_config idiom). With NO
    operator_id the GUC stays unset and the scoped views match nothing — fail-closed (pre-VT-377
    callers that only probe permissions/columns are unaffected).

    FAZAL break-glass: ``operator_id == FAZAL_OWNER_UUID`` (the admin identity that already
    passes `operator_assigned` without a row) delegates to :func:`vtr_admin_connection` — the
    ADMIN tier keeps all-tenants via the mig-134 role leg. The view scoping is DEFENSE IN DEPTH
    behind `require_vtr_action`'s per-tenant gate, not a replacement for it."""
    fazal = _fazal_owner_uuid()
    if operator_id and fazal and operator_id == fazal:
        with vtr_admin_connection(pool=pool) as conn:
            yield conn
        return
    active_pool = pool if pool is not None else get_pool()
    with active_pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET ROLE app_vtr_role")
        try:
            if operator_id is None:
                yield conn
            else:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT set_config(%s, %s, true)",
                            (_VTR_OPERATOR_GUC, operator_id),
                        )
                    yield conn
        finally:
            with conn.cursor() as cur:
                cur.execute("RESET ROLE")


@contextmanager
def vtr_admin_connection(*, pool: Any | None = None) -> Iterator[Any]:
    """Check out a pooled connection with `SET ROLE app_vtr_admin_role` for the duration.

    The ADMIN tier (Fazal=VTR#1: the all-tenants VTR digest, break-glass console/run-control
    reads): mig-134's view predicate opens every tenant for `current_user =
    'app_vtr_admin_role'` — role IS the mechanism (Cowork ruling, no bypass flags). The role
    still holds ZERO raw-table grants (mig-130/134): the de-identified views +
    vtr_admin_batch_drafts are its only doors, so the PII boundary is unchanged.

    NOTE: the params drill-in (`vtr-batch-drafts`, ops_vtr_console) deliberately does NOT use
    this helper — it keeps its own `SET LOCAL ROLE app_vtr_admin_role` inside the SAME txn as
    its audit-before-read INSERT (no silent break-glass). This helper is for the all-tenants
    READ tier only. Mirrors :func:`vtr_connection`'s SET ROLE / RESET ROLE hygiene."""
    active_pool = pool if pool is not None else get_pool()
    with active_pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET ROLE app_vtr_admin_role")
        try:
            yield conn
        finally:
            with conn.cursor() as cur:
                cur.execute("RESET ROLE")
