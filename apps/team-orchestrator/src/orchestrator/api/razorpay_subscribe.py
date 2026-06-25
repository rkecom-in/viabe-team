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
    TierNotOfferedError,
    UnknownPlanError,
    assert_tier_offered,
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


# VT-424 — Razorpay idempotency header. Razorpay's idempotency support is per-endpoint and
# documented for Payouts/Refunds (``X-Payout-Idempotency`` / ``X-Refund-Idempotency``); the
# subscriptions endpoint is NOT documented to honour it. We send the vendor-convention header
# anyway (cheap; if the endpoint DOES dedupe, it's the cleanest guard), BUT the load-bearing
# double-subscribe protection is the DB before-vendor check + the per-tenant advisory lock + the
# ``razorpay_subscription_id`` UNIQUE — NOT this header. The PRE-LIVE canary (below) must prove
# whether Razorpay actually dedupes; if it does NOT, the DB guards still hold (no orphan).
_IDEMPOTENCY_HEADER = "Idempotency-Key"  # Cowork VT-424 directive; standard idempotency header (Razorpay subscriptions endpoint is undocumented for it — the LIVE canary proves dedupe; DB guards hold regardless)


class RazorpayKeysNotConfiguredError(RuntimeError):
    """The live Razorpay API key id/secret env vars are unset (NEEDS-FAZAL for LIVE).

    Maps to a 503 (same fail-closed contract as PlanIdNotConfiguredError) — a missing key is not
    a caller-fixable error, and we must NEVER fall back to a stub at the money layer."""


def _get_razorpay_client() -> Any:
    """Build a live Razorpay client from env keys, BY REFERENCE (CL-431/Rule-18 — the values are
    read straight into the SDK constructor; they never touch a log/stdout/this process's surfaced
    context). Lazy-imports the SDK so the module loads dep-less (the razorpay package is NOT a
    smoke-test dep). Raises RazorpayKeysNotConfiguredError (→503) when keys are absent — fail-closed,
    never a stub fallback."""
    key_id = os.environ.get("TEAM_RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("TEAM_RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        raise RazorpayKeysNotConfiguredError(
            "TEAM_RAZORPAY_KEY_ID/SECRET unset — live Razorpay keys are NEEDS-FAZAL (LIVE)"
        )
    import razorpay  # lazy — NEEDS-FAZAL dep; keeps the module dep-less-smoke importable

    return razorpay.Client(auth=(key_id, key_secret))


def _create_razorpay_subscription(
    plan: ResolvedPlan, tenant_id: str, idempotency_key: str, client: Any | None = None
) -> dict[str, str]:
    """Create the REAL Razorpay subscription (VT-424 — replaces the ``sub_stub_*`` STUB).

    Calls ``client.subscription.create(data, headers={'Idempotency-Key': <key>})`` with the
    plan_id from config, the per-attempt idempotency key in both the header AND ``notes`` (so the
    subscription is greppable to its conversion even if the header is not honoured), and the
    tenant bound via ``notes.tenant_id``. ``client`` is INJECTABLE — tests pass a stub so no
    network/key is needed; the live path builds it from env (:func:`_get_razorpay_client`), which
    503s fail-closed when keys are absent.

    VT-352 F2 — the idempotency key keys a vendor RETRY to the same subscription: if the vendor
    create succeeds but the DB commit then fails (rollback), the retry sends the SAME key. IF
    Razorpay honours it, the vendor returns the SAME subscription instead of a second (orphaned)
    one; if it does NOT (see ``_IDEMPOTENCY_HEADER`` note), the DB before-vendor check + advisory
    lock + UNIQUE still prevent a second BOUND subscription, and :func:`reconcile_subscription_orphans`
    detects any vendor orphan for manual reconcile.

    PRE-LIVE ACCEPTANCE (Cowork sharpening — NEEDS-FAZAL): a REAL-API (test-mode) canary at the
    TEAM_RAZORPAY_LIVE cutover MUST prove whether Razorpay honours the idempotency key on the
    SUBSCRIPTIONS endpoint (same key → same sub). "Razorpay supports this" is verified against the
    live endpoint, NOT assumed. If it does NOT honour it, the pre-committed-intent + the detect-only
    orphan sweep are the fallback before flipping live.
    """
    rzp = client if client is not None else _get_razorpay_client()
    data = {
        "plan_id": plan.razorpay_plan_id,
        "total_count": plan.total_count,
        "quantity": 1,
        "customer_notify": 1,
        # Bind the subscription to its tenant + conversion attempt at the vendor for reconciliation.
        "notes": {"tenant_id": tenant_id, "idempotency_key": idempotency_key},
    }
    sub = rzp.subscription.create(data, headers={_IDEMPOTENCY_HEADER: idempotency_key})
    sub_id = sub.get("id") if isinstance(sub, dict) else getattr(sub, "id", None)
    if not sub_id:
        # No partial state to clean up (the DB write hasn't run); surface a clean error so the txn
        # rolls back rather than binding a malformed/empty subscription id.
        raise RuntimeError("razorpay.subscription.create returned no subscription id")
    cust_id = sub.get("customer_id") if isinstance(sub, dict) else getattr(sub, "customer_id", None)
    return {"subscription_id": str(sub_id), "customer_id": str(cust_id) if cust_id else ""}


@router.post("/api/orchestrator/razorpay-subscribe")
def razorpay_subscribe(
    body: RazorpaySubscribeBody,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Create (or return the existing) Razorpay subscription for a tenant. 403 bad
    secret OR tier-not-offered (VT-429 launch gate); 400 unknown plan_tier; 503 plan-id OR
    live keys not configured (NEEDS-FAZAL, fail-closed — never a stub). Returns
    ``{status: created|exists, razorpay_subscription_id}``. Never flips phase."""
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="invalid internal secret")

    # VT-429 — single-plan launch gate (fail-closed). BEFORE resolve_plan + BEFORE any vendor
    # call: a plan_tier not in the server-side offered_tiers allowlist → 403, NO subscription, NO
    # vendor call. Never trust the client to send only an offered tier; an empty/absent
    # offered_tiers config is default-deny (offer-nothing), never offer-all.
    try:
        assert_tier_offered(body.plan_tier)
    except TierNotOfferedError:
        raise HTTPException(status_code=403, detail="tier not offered at launch") from None

    try:
        plan = resolve_plan(body.plan_tier)
    except UnknownPlanError:
        raise HTTPException(status_code=400, detail="unknown plan_tier") from None
    except PlanIdNotConfiguredError:
        # The Razorpay plan ID is NEEDS-FAZAL (LIVE). Not an error the caller can fix.
        raise HTTPException(status_code=503, detail="plan not configured") from None

    tenant_id = body.tenant_id
    try:
        return _do_subscribe(plan, body, tenant_id)
    except RazorpayKeysNotConfiguredError:
        # Live keys absent — same fail-closed contract as a missing plan-id (NEEDS-FAZAL). The
        # txn rolled back inside _do_subscribe, so there is NO partial state (no jti consumed, no
        # subscriptions row). Never a stub fallback at the money layer.
        raise HTTPException(status_code=503, detail="razorpay keys not configured") from None


def _do_subscribe(
    plan: ResolvedPlan, body: RazorpaySubscribeBody, tenant_id: str
) -> dict[str, Any]:
    """The transactional core (split out so the endpoint can map RazorpayKeysNotConfiguredError →
    503 cleanly). Returns the same shape the endpoint returns."""
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
            # First claimer — create at the vendor (REAL razorpay.subscription.create, VT-424)
            # inside the lock so a racing caller can't also create. The live call serializes on the
            # advisory lock (acceptable — the point is exactly-one vendor subscription).
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
