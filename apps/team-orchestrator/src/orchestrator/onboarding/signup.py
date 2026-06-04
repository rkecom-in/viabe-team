"""VT-82 — owner signup: atomic service_role tenant-create core.

The signup mini-phase's persistence seam. Creates the tenant row + the owner
consent_records proof + trial init in ONE transaction, PRE-tenant-context
(service_role pool, no GUC — tenants is the bootstrap table, NOT a wrapper site).

SCOPE (Cowork plan-approved, backend-first): this is the field-agnostic create core.
The HTTP endpoint's field VALIDATION (phone E.164 / blocklist), the city-capture →
``set_tenant_city_tier`` fold (VT-317), and the welcome WhatsApp send are HELD until
Fazal relays the exact field set — they wrap this core, they don't change it.

Founding tier is a STUB (default 'founding' + injectable seam) until VT-10.6's atomic
counter lands — never a half-built counter (no-stale).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast
from uuid import UUID

# .../team-orchestrator/src/orchestrator/onboarding/signup.py → parents[3] = team-orchestrator
_DISCLOSURES = (
    Path(__file__).resolve().parents[3] / "config" / "disclosure_versions.yaml"
)


@dataclass(frozen=True)
class SignupResult:
    tenant_id: UUID
    created: bool  # False ⇒ duplicate whatsapp_number (endpoint → 409)
    plan_tier: str | None  # None on a duplicate (no new tenant created)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_founding_counter() -> bool:
    """STUB until VT-10.6: every signup is 'founding'. Replace with the atomic
    founding-counter CAS when it lands (injected via ``founding_counter_fn``)."""
    # TODO(VT-10.6): gate on the atomic founding-tier counter (space → True).
    return True


def _disclosure_versions() -> tuple[str, str]:
    """(dpdpa_version, residency_version) from config — never free strings."""
    import yaml

    cfg = yaml.safe_load(_DISCLOSURES.read_text(encoding="utf-8"))
    return cfg["dpdpa"]["current"], cfg["residency"]["current"]


def create_signup_tenant(
    *,
    business_name: str,
    whatsapp_number: str,
    preferred_language: str,
    consent_dpdpa: bool,
    consent_residency: bool,
    founding_counter_fn: Callable[[], bool] | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> SignupResult:
    """Atomically create the signup tenant + consent proof + trial init.

    One service_role txn: INSERT tenants (ON CONFLICT whatsapp_number → no row, the
    duplicate case) + INSERT consent_records + emit TENANT_CREATED. Drains post-commit.
    Pillar-7: BOTH consents must be true — a false consent never reaches here (the
    endpoint rejects it); this core asserts it as defense-in-depth.
    """
    if not (consent_dpdpa and consent_residency):
        raise ValueError("signup requires both DPDPA and residency consent (Pillar 7)")
    if not whatsapp_number:
        raise ValueError("whatsapp_number is the mandatory tenant identity")

    from orchestrator.graph import get_pool
    from orchestrator.knowledge.kg_emit import drain_kg_events, emit_kg_event
    from orchestrator.knowledge.kg_vocab import KgEventType

    now = (now_fn or _utcnow)()
    plan_tier = "founding" if (founding_counter_fn or _default_founding_counter)() else "standard"
    dpdpa_version, residency_version = _disclosure_versions()

    pool = get_pool()
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            """
            INSERT INTO tenants
                (business_name, plan_tier, phase, whatsapp_number, preferred_language,
                 signed_up_at, trial_started_at, phase_entered_at, created_via)
            VALUES (%s, %s, 'onboarding', %s, %s, %s, %s, %s, 'web')
            ON CONFLICT (whatsapp_number) WHERE whatsapp_number IS NOT NULL
            DO NOTHING
            RETURNING id
            """,
            (business_name, plan_tier, whatsapp_number, preferred_language,
             now, now, now),
        ).fetchone()

        if row is None:
            # Duplicate whatsapp_number — the unique identity already signed up.
            existing = conn.execute(
                "SELECT id FROM tenants WHERE whatsapp_number = %s", (whatsapp_number,)
            ).fetchone()
            tid = UUID(str(cast("dict[str, Any]", existing)["id"]))
            return SignupResult(tenant_id=tid, created=False, plan_tier=None)

        tid = UUID(str(cast("dict[str, Any]", row)["id"]))
        conn.execute(
            """
            INSERT INTO consent_records
                (tenant_id, consent_dpdpa, consent_residency,
                 dpdpa_version, residency_version, signed_up_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (str(tid), consent_dpdpa, consent_residency,
             dpdpa_version, residency_version, now),
        )
        # CL-390: emit the real business_name only (no phone in the durable payload).
        emit_kg_event(conn, KgEventType.TENANT_CREATED, tid, {
            "business_name": business_name,
        })

    drain_kg_events(tid)
    return SignupResult(tenant_id=tid, created=True, plan_tier=plan_tier)


__all__ = ["SignupResult", "create_signup_tenant"]
