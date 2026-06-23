"""VT-267 PR-A2 — tenant provisioning (dual-entry, Fazal D1 identity model).

Fazal D1 (2026-06-02): the business_contact (WhatsApp number) is the MANDATORY
tenant identity (globally unique, migration 066); owner_contact is OPTIONAL
(escalation/severe-interaction only, nullable). Dual entry — a web/QR link OR an
inbound WhatsApp message — both resolve to the business number as the tenant key,
so the SAME number yields the SAME tenant (merge, not a new row). The business
number is OTP-verified at create time by the owner-surface (VT-250); this step is
the durable persistence seam the verified-entry calls.

This is the D1-dependent piece held out of PR-A. The floor/intent/method_selector
parts of PR-B run on the tenant this returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID


@dataclass(frozen=True)
class TenantProvisionResult:
    # VT-408: tenant_id is None when provisioning was REFUSED — an unknown/new inbound number
    # without a verified GSTIN gets NO tenant (verify-first signup is the only door). created
    # is True only for a real new row; a refused provision is created=False, tenant_id=None,
    # provisioned=False, so the caller replies with the "verify at signup" directive.
    tenant_id: UUID | None
    created: bool  # True = a new tenant; False = merged into an existing one OR refused
    provisioned: bool = True  # False ⇒ VT-408 refused an unknown unverified inbound number


def create_tenant_if_unknown(
    business_contact: str,
    *,
    business_name: str | None = None,
    owner_contact: str | None = None,
    created_via: str | None = None,
    verified: bool = False,
) -> TenantProvisionResult:
    """Idempotent provision keyed on ``business_contact`` (the unique WhatsApp
    identity, mig 066). Same number → the existing tenant (merge); a newly-supplied
    ``owner_contact`` backfills if the row had none. Returns (tenant_id, created,
    provisioned).

    **VT-408 (CL-442) — the inbound backdoor is CLOSED for NEW/UNKNOWN numbers.** A no-GST
    business gets nothing, neither paid nor trial. So an UNKNOWN ``business_contact`` is
    provisioned ONLY when ``verified=True`` (a gated web/QR entry that has already passed the
    OTP + GSTIN verify). The inbound WhatsApp ingress passes ``verified=False`` (the default)
    → the unknown number is REFUSED (no row, no PII, no trial) and the caller replies with the
    "verify at signup" directive (signup_gate.gate_copy('inbound_directive', lang)). A KNOWN
    number is UNAFFECTED — it already passed the gate at signup, so the merge/backfill path
    proceeds unconditionally (this is the legitimate already-known-tenant flow the ruling
    preserves; an inbound message from an existing tenant must still resolve to its tenant).

    ``business_name`` defaults to the number until onboarding captures the real name
    (tenants.business_name is NOT NULL). New tenants land phase='onboarding'. Raises
    ValueError if ``business_contact`` is empty (it is the mandatory identity).
    """
    if not business_contact:
        raise ValueError("business_contact (WhatsApp number) is mandatory — it is the tenant identity")

    from orchestrator.graph import get_pool

    pool = get_pool()
    # VT-408: refuse to CREATE a new tenant for an unknown number unless it came through the
    # verified (OTP + GSTIN) gate. A known number is exempt — it is already a verified tenant
    # (merge path preserved). The pre-check is a cheap existence read; the actual create stays
    # the atomic ON CONFLICT below (so a concurrent create still merges, never double-inserts).
    if not verified:
        with pool.connection() as _conn:
            known = _conn.execute(
                "SELECT id FROM tenants WHERE whatsapp_number = %s LIMIT 1",
                (business_contact,),
            ).fetchone()
        if known is None:
            # Unknown + unverified → REFUSE. No tenant, no PII persisted (DPDP posture).
            return TenantProvisionResult(tenant_id=None, created=False, provisioned=False)

    name = business_name or business_contact  # placeholder until onboarding fills it
    # VT-65 PR-2: INSERT + KG emit in one txn (atomic — the kg_events outbox row
    # only lands if the tenant INSERT commits). Single-statement site → benign wrap.
    with pool.connection() as conn, conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenants
                    (business_name, plan_tier, phase, whatsapp_number, owner_contact, created_via)
                VALUES (%s, 'founding', 'onboarding', %s, %s, %s)
                ON CONFLICT (whatsapp_number) WHERE whatsapp_number IS NOT NULL
                DO UPDATE SET
                    owner_contact = COALESCE(EXCLUDED.owner_contact, tenants.owner_contact)
                RETURNING id, (xmax = 0) AS created
                """,
                (name, business_contact, owner_contact, created_via),
            )
            row = cast("dict[str, Any]", cur.fetchone())
        tenant_uuid = UUID(str(row["id"]))
        from orchestrator.knowledge.kg_emit import emit_kg_event
        from orchestrator.knowledge.kg_vocab import KgEventType

        # VT-315 / CL-390: emit the REAL business_name (None until onboarding
        # captures it) — NEVER the `name` phone-fallback. The tenants row keeps
        # the phone fallback (tenant identity); the durable kg_events payload
        # must not carry the raw phone. _h_tenant_created tolerates None.
        emit_kg_event(conn, KgEventType.TENANT_CREATED, tenant_uuid, {
            "business_name": business_name,
        })

    from orchestrator.knowledge.kg_emit import drain_kg_events

    drain_kg_events(tenant_uuid)
    return TenantProvisionResult(tenant_id=tenant_uuid, created=bool(row["created"]))


__all__ = ["TenantProvisionResult", "create_tenant_if_unknown"]
