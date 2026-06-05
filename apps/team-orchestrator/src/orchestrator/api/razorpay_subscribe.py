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


def _verify_internal_secret(provided: str | None) -> bool:
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _create_razorpay_subscription(plan: ResolvedPlan, tenant_id: str) -> dict[str, str]:
    """STUB — NEEDS-FAZAL. The live Razorpay subscriptions.create(plan_id=...) call goes
    here, gated by LIVE keys (VT-93-N1 + VT-329 + VT-330 + this row's
    idempotency-before-vendor). The stub returns deterministic fake IDs so the flow +
    canary exercise end-to-end without a vendor. Razorpay returns real unique IDs live."""
    # NEEDS-FAZAL: replace with the live Razorpay subscriptions.create + customer create.
    return {
        # Unique per call (LIVE Razorpay sub IDs are unique per subscription) so a
        # re-subscribe after cancel can't collide on the razorpay_subscription_id UNIQUE.
        "subscription_id": f"sub_stub_{tenant_id}_{uuid4().hex[:10]}",
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
            vendor = _create_razorpay_subscription(plan, tenant_id)
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
