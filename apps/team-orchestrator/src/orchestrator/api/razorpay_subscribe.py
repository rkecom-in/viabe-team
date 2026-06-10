"""Razorpay subscription creation (VT-331) — orchestrator-authoritative.

team-web's /subscribe authenticates the owner (requireFazal, server-derived tenant)
and forwards {tenant_id, plan_tier} ONLY. This endpoint is the money-authoritative
layer (Cowork Q1): it resolves plan_tier -> {plan_id, amount} from its OWN config,
makes the Razorpay vendor call (STUBBED; LIVE is NEEDS-FAZAL), and writes
``subscriptions`` (service-role). It does NOT flip phase — trial->paid stays
webhook-only (the VT-89 payment.captured path is the single conversion source).

Idempotency + concurrency (the VT-93-N1 lesson): a per-tenant advisory lock serializes
concurrent creates, and the existing-subscription check runs BEFORE the vendor call, so
a double-POST race can NEVER create two real Razorpay subscriptions at LIVE. The
``subscriptions.razorpay_subscription_id`` UNIQUE (mig 003) is the backstop.

Service-role only: ``subscriptions`` predates the app_role grant — writes here are the
privileged pool with an explicit ``WHERE tenant_id`` (mirrors razorpay_ingress).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel

from orchestrator.billing.plans import (
    PlanIdNotConfiguredError,
    ResolvedPlan,
    UnknownPlanError,
    resolve_plan,
)
from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()


class RazorpaySubscribeBody(BaseModel):
    """Forwarded by team-web after requireFazal auth — tenant is server-derived there,
    plan_tier is the owner's choice. NO plan_id / amount crosses the boundary (Q1)."""

    tenant_id: str
    plan_tier: str
    # VT-332: the trial-end token's jti, forwarded by team-web after it verifies the deep-link
    # token. Present ONLY on the trial-end deep-link path; consumed single-use below. None on the
    # in-app subscribe path (no token).
    jti: str | None = None


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _create_razorpay_subscription(
    plan: ResolvedPlan, tenant_id: str, idempotency_key: str
) -> dict[str, str]:
    """STUB — NEEDS-FAZAL. Live: ``razorpay.subscription.create(plan_id=...,
    headers={'Idempotency-Key': idempotency_key})``, gated by LIVE keys (VT-93-N1 + VT-329 +
    VT-330 + this row's idempotency-before-vendor).

    VT-352 F2 — the Idempotency-Key prevents a vendor ORPHAN: if the vendor create succeeds but the
    DB commit fails (rollback), the retry sends the SAME key, so Razorpay returns the SAME
    subscription instead of creating a second (orphaned) one. The stub MODELS that: the id is
    DERIVED from the key, so the same key → the same id (a retry is a vendor no-op), while a new
    authorized attempt (new jti → new key) → a new id (a re-subscribe after cancel can't collide on
    the razorpay_subscription_id UNIQUE).

    PRE-LIVE ACCEPTANCE (Cowork sharpening): a REAL-API canary at the TEAM_RAZORPAY_LIVE cutover
    MUST prove Razorpay actually honors the Idempotency-Key (same key → same sub) — "Razorpay
    supports this" is verified against the live endpoint, not assumed; if it does NOT honor it, fall
    back to a pre-committed intent row before flipping live. The detect-only sweep
    (:func:`reconcile_subscription_orphans`) is the defense-in-depth backstop either way.
    """
    # NEEDS-FAZAL: replace with the live Razorpay create + the Idempotency-Key header.
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:10]
    return {
        "subscription_id": f"sub_stub_{tenant_id}_{key_hash}",
        "customer_id": f"cust_stub_{tenant_id}",
    }


@router.post("/api/orchestrator/razorpay-subscribe")
def razorpay_subscribe(
    body: RazorpaySubscribeBody,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Create (or return the existing) Razorpay subscription for a tenant. 403 bad
    secret; 400 unknown plan_tier; 503 plan-id not configured (NEEDS-FAZAL). Returns
    ``{status: created|exists, razorpay_subscription_id}``. Never flips phase."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="invalid internal secret")

    try:
        plan = resolve_plan(body.plan_tier)
    except UnknownPlanError:
        raise HTTPException(status_code=400, detail="unknown plan_tier") from None
    except PlanIdNotConfiguredError:
        # The Razorpay plan ID is NEEDS-FAZAL (LIVE). Not an error the caller can fix.
        raise HTTPException(status_code=503, detail="plan not configured") from None

    tenant_id = body.tenant_id
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        with conn.transaction():
            # Concurrency: serialize per-tenant creates (VT-93-N1 pattern). A double-POST
            # race blocks here; the 2nd caller proceeds only after the 1st commits.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"subscribe:{tenant_id}",))
            # VT-332 single-use (the keystone): consume the trial-end token's jti ATOMICALLY —
            # inside the lock, BEFORE the idempotency check + the vendor call. A replay (an
            # already-consumed jti) → rowcount 0 → 403, so a replayed PAYMENT deep-link never
            # reaches the vendor and never creates a 2nd subscription. The consume shares this
            # txn with the create: jti is consumed IFF the subscribe commits (a failed create
            # rolls back the consume → the token stays usable; a success spends it once).
            if body.jti is not None:
                cur.execute(
                    "INSERT INTO consumed_subscribe_tokens (jti, tenant_id, plan_tier) "
                    "VALUES (%s, %s, %s) ON CONFLICT (jti) DO NOTHING",
                    (body.jti, tenant_id, body.plan_tier),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=403, detail="token already used")
            # Idempotency BEFORE the vendor call: an existing bound subscription wins —
            # no second Razorpay subscription is ever created.
            cur.execute(
                "SELECT razorpay_subscription_id FROM subscriptions "
                "WHERE tenant_id = %s AND razorpay_subscription_id IS NOT NULL "
                "AND status = 'active' "
                "LIMIT 1",
                (tenant_id,),
            )
            existing = cur.fetchone()
            if existing is not None:
                return {
                    "status": "exists",
                    "razorpay_subscription_id": existing["razorpay_subscription_id"],
                }
            # First claimer — create at the vendor (STUB) inside the lock so a racing
            # caller can't also create. Stub is instant; the live call serializes on the
            # lock (acceptable — the point is exactly-one vendor subscription).
            # VT-352 F2: the Idempotency-Key keys a vendor retry to the same subscription. jti
            # (the trial-end token, the customer money path) is the per-attempt key — a new
            # authorized attempt = new jti = new key; a retry of THIS attempt = same key = same
            # sub (no orphan). No-jti (internal) path falls to the advisory lock + the
            # reconcile-orphans sweep, since there's no per-attempt token to key on.
            idem_key = (
                f"subscribe:{tenant_id}:{body.jti}"
                if body.jti is not None
                else f"subscribe:{tenant_id}:{uuid4().hex}"
            )
            vendor = _create_razorpay_subscription(plan, tenant_id, idem_key)
            cur.execute(
                "INSERT INTO subscriptions "
                "(tenant_id, razorpay_subscription_id, razorpay_customer_id, "
                " razorpay_plan_id, status, started_at) "
                "VALUES (%s, %s, %s, %s, 'active', now())",
                (
                    tenant_id,
                    vendor["subscription_id"],
                    vendor["customer_id"],
                    plan.razorpay_plan_id,
                ),
            )
    logger.info("razorpay-subscribe: created tenant=%s tier=%s", tenant_id, body.plan_tier)
    return {"status": "created", "razorpay_subscription_id": vendor["subscription_id"]}


def reconcile_subscription_orphans(vendor_subscription_ids: list[str]) -> list[str]:
    """VT-352 F2 — DETECT-ONLY vendor-orphan reconciliation (defense-in-depth behind the
    Idempotency-Key). Given the live Razorpay subscription ids (the live caller passes
    ``razorpay.subscription.all()``; the canary injects a list), find any vendor subscription with
    NO matching ``subscriptions`` row — an orphan from a commit-after-vendor failure the
    idempotency-key didn't cover — and ALERT Fazal.

    NO auto-cancel / auto-remediation (Cowork sharpening): an unattended money action on a
    half-known subscription is how orphan-handling creates a NEW incident; a human reconciles.
    Returns the orphan ids. Service-role (subscriptions predates the app_role grant)."""
    if not vendor_subscription_ids:
        return []
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT razorpay_subscription_id FROM subscriptions "
            "WHERE razorpay_subscription_id = ANY(%s)",
            (vendor_subscription_ids,),
        )
        known = {r["razorpay_subscription_id"] for r in cur.fetchall()}
    orphans = [s for s in vendor_subscription_ids if s not in known]
    if orphans:
        try:
            from orchestrator.alerts.clients import alert_fazal as _alert_fazal

            _alert_fazal(
                f"⚠️ VT-352 vendor-orphan(s): {len(orphans)} Razorpay subscription(s) with NO DB "
                "row — likely a commit-after-vendor failure. RECONCILE MANUALLY (no auto-action): "
                + ", ".join(orphans[:10])
            )
        except Exception:
            logger.exception("VT-352 orphan alert failed")
    logger.info(
        "VT-352 orphan reconcile: %d vendor sub(s), %d orphan(s)",
        len(vendor_subscription_ids),
        len(orphans),
    )
    return orphans
