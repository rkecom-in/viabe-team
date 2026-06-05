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

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, cast
from uuid import UUID

logger = logging.getLogger(__name__)

# Structural India-mobile E.164: +91 then a 10-digit number starting 6-9. Dep-free
# (phonenumbers is not installed; a fuller-validation upgrade is a follow-up).
_PHONE_RE = re.compile(r"^\+91[6-9]\d{9}$")
_LANGUAGES = frozenset({"en", "hi"})
_TRIAL_DAYS = 14

# .../team-orchestrator/src/orchestrator/onboarding/signup.py → parents[3] = team-orchestrator
# .../team-orchestrator/src/orchestrator/onboarding/signup.py → parents[3] = team-orchestrator
_CONFIG = Path(__file__).resolve().parents[3] / "config"
_DISCLOSURES = _CONFIG / "disclosure_versions.yaml"
_BUSINESS_TYPES = _CONFIG / "business_types.yaml"


def valid_business_types() -> frozenset[str]:
    """The constrained business_type taxonomy keys (VT-82 — NOT free text). Coarse
    by design so L3/k-anon cohorts (business_type × city_tier) stay populated."""
    import yaml

    cfg = yaml.safe_load(_BUSINESS_TYPES.read_text(encoding="utf-8"))
    return frozenset(bt["key"] for bt in cfg["business_types"])


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
    business_type: str,
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
    if business_type not in valid_business_types():
        raise ValueError(f"business_type {business_type!r} not in the taxonomy")

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
                 business_type, signed_up_at, trial_started_at, phase_entered_at,
                 created_via)
            VALUES (%s, %s, 'onboarding', %s, %s, %s, %s, %s, %s, 'web')
            ON CONFLICT (whatsapp_number) WHERE whatsapp_number IS NOT NULL
            DO NOTHING
            RETURNING id
            """,
            (business_name, plan_tier, whatsapp_number, preferred_language,
             business_type, now, now, now),
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


# --------------------------------------------------------------------------- #
# Endpoint orchestration: validate → create → city_tier → owner_name → welcome
# --------------------------------------------------------------------------- #

class SignupError(Exception):
    """Validation / conflict failure. ``code`` maps to an HTTP status at the route:
    'invalid_*' / 'consent' → 400; 'duplicate' → 409."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SignupInput:
    business_name: str
    owner_name: str
    whatsapp_number: str
    preferred_language: str
    city: str
    business_type: str
    consent_dpdpa: bool
    consent_residency: bool


@dataclass(frozen=True)
class SignupOutcome:
    tenant_id: UUID
    plan_tier: str
    city_tier: str
    welcome_sent: bool


def _load_blocklist() -> list[str]:
    import yaml

    cfg = yaml.safe_load((_CONFIG / "signup_blocklist.yaml").read_text(encoding="utf-8"))
    return [t.casefold() for t in (cfg.get("business_name") or [])]


def _validate(inp: SignupInput) -> None:
    if not (inp.consent_dpdpa and inp.consent_residency):
        raise SignupError("consent", "both DPDPA and residency consent are required")
    if not _PHONE_RE.match(inp.whatsapp_number):
        raise SignupError("invalid_phone", "whatsapp_number must be a +91 mobile (E.164)")
    if inp.preferred_language not in _LANGUAGES:
        raise SignupError("invalid_language", "preferred_language must be 'en' or 'hi'")
    if not inp.city.strip():
        raise SignupError("invalid_city", "city is required")
    if inp.business_type not in valid_business_types():
        raise SignupError("invalid_business_type", "business_type not in the taxonomy")
    blocked = _load_blocklist()
    for field in (inp.business_name, inp.owner_name):
        folded = (field or "").casefold()
        if not folded.strip():
            raise SignupError("invalid_name", "business_name and owner_name are required")
        if any(term in folded for term in blocked):
            raise SignupError("invalid_name", "name contains a disallowed term")


def _default_welcome(
    tenant_id: UUID, whatsapp_number: str, language: str,
    owner_name: str, trial_end: datetime,
) -> bool:
    """STUB owner welcome-send seam (WABA stubbed/injectable, Cowork). The real
    owner-WABA send is gate-live (same posture as customer-comms); until then this
    logs intent. Tests inject a real recorder. NON-terminal: a failure here never
    rolls back the signup."""
    # TODO(owner-WABA): send `team_welcome` (lang SID) to the owner's number with
    # {owner_name, trial_end_date} once the owner-WABA delivery path is live.
    logger.info(
        "signup: welcome queued tenant=%s lang=%s (owner-WABA send is gate-live)",
        tenant_id, language,
    )
    return True


def run_signup(
    inp: SignupInput,
    *,
    welcome_send_fn: Callable[..., bool] | None = None,
    founding_counter_fn: Callable[[], bool] | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> SignupOutcome:
    """The signup orchestration. Validate (→ SignupError on any field/consent) →
    atomic create (→ SignupError 'duplicate' on a repeat whatsapp_number) →
    set_tenant_city_tier (closes VT-317) → merge owner_name into business_profile →
    welcome send (injectable, non-terminal)."""
    _validate(inp)
    now = (now_fn or _utcnow)()

    res = create_signup_tenant(
        business_name=inp.business_name,
        whatsapp_number=inp.whatsapp_number,
        preferred_language=inp.preferred_language,
        business_type=inp.business_type,
        consent_dpdpa=inp.consent_dpdpa,
        consent_residency=inp.consent_residency,
        founding_counter_fn=founding_counter_fn,
        now_fn=now_fn,
    )
    if not res.created:
        raise SignupError("duplicate", "this whatsapp_number is already registered")

    # VT-317: capture the city → coarsen → tenants.city_tier (raw city discarded).
    from orchestrator.privacy.coarsening import set_tenant_city_tier

    city_tier = str(set_tenant_city_tier(res.tenant_id, inp.city))

    # owner_name lives on business_profile (where get_business_profile reads it),
    # merged (not clobbered) so later enrichment is preserved.
    from orchestrator.knowledge.l1 import upsert_business_profile

    upsert_business_profile(res.tenant_id, {"owner_name": inp.owner_name})

    trial_end = now + timedelta(days=_TRIAL_DAYS)
    sent = (welcome_send_fn or _default_welcome)(
        res.tenant_id, inp.whatsapp_number, inp.preferred_language,
        inp.owner_name, trial_end,
    )

    return SignupOutcome(
        tenant_id=res.tenant_id,
        plan_tier=cast(str, res.plan_tier),
        city_tier=city_tier,
        welcome_sent=bool(sent),
    )


__all__ = [
    "SignupError",
    "SignupInput",
    "SignupOutcome",
    "SignupResult",
    "create_signup_tenant",
    "run_signup",
    "valid_business_types",
]
