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
    tenant_id: UUID
    created: bool  # True = a new tenant; False = merged into an existing one


def create_tenant_if_unknown(
    business_contact: str,
    *,
    business_name: str | None = None,
    owner_contact: str | None = None,
    created_via: str | None = None,
) -> TenantProvisionResult:
    """Idempotent provision keyed on ``business_contact`` (the unique WhatsApp
    identity, mig 066). Same number → the existing tenant (merge); a newly-supplied
    ``owner_contact`` backfills if the row had none. Returns (tenant_id, created).

    ``business_name`` defaults to the number until onboarding captures the real name
    (tenants.business_name is NOT NULL). New tenants land phase='onboarding'. Raises
    ValueError if ``business_contact`` is empty (it is the mandatory identity).
    """
    if not business_contact:
        raise ValueError("business_contact (WhatsApp number) is mandatory — it is the tenant identity")

    from orchestrator.graph import get_pool

    name = business_name or business_contact  # placeholder until onboarding fills it
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
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
    return TenantProvisionResult(
        tenant_id=UUID(str(row["id"])), created=bool(row["created"])
    )


__all__ = ["TenantProvisionResult", "create_tenant_if_unknown"]
