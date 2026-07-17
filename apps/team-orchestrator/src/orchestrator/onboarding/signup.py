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

import json
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
# VT-449: the DPDP consent/purpose string (≥20 chars, required) on the MCA company lookup at signup.
_MCA_SIGNUP_REASON = "Owner business identity verification during Viabe signup"

# .../team-orchestrator/src/orchestrator/onboarding/signup.py → parents[3] = team-orchestrator
# .../team-orchestrator/src/orchestrator/onboarding/signup.py → parents[3] = team-orchestrator
_CONFIG = Path(__file__).resolve().parents[3] / "config"
_DISCLOSURES = _CONFIG / "disclosure_versions.yaml"
_BUSINESS_TYPES = _CONFIG / "business_types.yaml"
_TRIAL_YAML = _CONFIG / "trial.yaml"


def _trial_days() -> int:
    """The authoritative trial length — config/trial.yaml ``trial_days`` (CL-433: 30), the SAME
    source the evaluator/sweep read. VT-371: a stale local ``_TRIAL_DAYS = 14`` here fed the
    ``team_welcome`` {{2}} trial-end date 16 days early; deriving from the shared config means
    the welcome can never drift from the machine that actually expires the trial."""
    import yaml

    cfg = yaml.safe_load(_TRIAL_YAML.read_text(encoding="utf-8"))
    return int(cfg["trial_days"])


def valid_business_types() -> frozenset[str]:
    """The constrained business_type taxonomy keys (VT-82 — NOT free text). Coarse
    by design so L3/k-anon cohorts (business_type × city_tier) stay populated."""
    import yaml

    cfg = yaml.safe_load(_BUSINESS_TYPES.read_text(encoding="utf-8"))
    return frozenset(bt["key"] for bt in cfg["business_types"])


def business_type_options() -> list[dict[str, str]]:
    """The taxonomy as {key, label_en, label_hi} rows (VT-96: the signup form's
    dropdown — single source of truth, no client-side drift). Public, non-PII."""
    import yaml

    cfg = yaml.safe_load(_BUSINESS_TYPES.read_text(encoding="utf-8"))
    return [
        {"key": bt["key"], "label_en": bt["label_en"], "label_hi": bt["label_hi"]}
        for bt in cfg["business_types"]
    ]


@dataclass(frozen=True)
class SignupResult:
    tenant_id: UUID
    created: bool  # False ⇒ duplicate whatsapp_number (endpoint → 409)
    plan_tier: str | None  # None on a duplicate (no new tenant created)
    city_tier: str | None  # None on a duplicate


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _disclosure_versions() -> tuple[str, str]:
    """(dpdpa_version, residency_version) from config — never free strings."""
    import yaml

    cfg = yaml.safe_load(_DISCLOSURES.read_text(encoding="utf-8"))
    return cfg["dpdpa"]["current"], cfg["residency"]["current"]


def create_signup_tenant(
    *,
    business_name: str,
    owner_name: str,
    whatsapp_number: str,
    preferred_language: str,
    city: str,
    business_type: str,
    consent_dpdpa: bool,
    consent_residency: bool,
    verified_gstin: str | None = None,
    verified_business_name: str | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> SignupResult:
    """Atomically create the signup tenant + consent proof + trial + city_tier +
    owner_name, in ONE service_role transaction.

    EVERYTHING-or-nothing (review: half-built-tenant fix): the tenants row (incl
    the coarsened city_tier, computed before the txn — raw city discarded, VT-317),
    consent_records, and the business_profile owner_name merge all commit together.
    A post-commit failure can no longer leave a burned whatsapp_number with NULL
    city_tier. ON CONFLICT → no row = the duplicate case (endpoint 409).
    Pillar-7: both consents required (defense-in-depth; the endpoint also gates).
    """
    if not (consent_dpdpa and consent_residency):
        raise ValueError("signup requires both DPDPA and residency consent (Pillar 7)")
    if not whatsapp_number:
        raise ValueError("whatsapp_number is the mandatory tenant identity")
    if business_type not in valid_business_types():
        raise ValueError(f"business_type {business_type!r} not in the taxonomy")

    from orchestrator.billing.founding_counter import try_claim_founding_slot
    from orchestrator.graph import get_pool
    from orchestrator.knowledge.kg_emit import drain_kg_events, emit_kg_event
    from orchestrator.knowledge.kg_vocab import KgEventType
    from orchestrator.privacy.coarsening import coarsen_city

    now = (now_fn or _utcnow)()
    # VT-94: default standard; upgraded to 'founding' IN-txn iff a counter slot is claimed.
    plan_tier = "standard"
    dpdpa_version, residency_version = _disclosure_versions()
    city_tier = str(coarsen_city(city))  # VT-317: raw city is discarded here.

    # VT-408: a signup tenant is verified BY CONSTRUCTION — run_signup's verify-then-create
    # gate is the ONLY door to this INSERT, so stamp gstin_verified (+ the authoritative
    # name / gstin / method / verified_at) in the SAME atomic txn. This is what the
    # transitions.py defense-in-depth activation gate reads; without it a legitimately
    # verified owner would be blocked at subscribe. Default stays 'unverified' for any
    # non-signup create path (mig 120) — only the gated signup stamps verified.
    verification_status = "gstin_verified" if verified_gstin else "unverified"
    verification_method = "gstin_lookup" if verified_gstin else None
    verified_at = now if verified_gstin else None

    pool = get_pool()
    with pool.connection() as conn, conn.transaction():
        # VT-677 D3: the signup form's EN/HI toggle is a UI-display PROXY, not an asked question —
        # it seeds the OBSERVED column (language_preference); preferred_language (the EXPLICIT
        # choice) stays NULL until the owner actually chooses (verbal override / settings). The
        # per-turn triage inference then refines the observed value from real usage.
        row = conn.execute(
            """
            INSERT INTO tenants
                (business_name, plan_tier, phase, whatsapp_number, language_preference,
                 business_type, city_tier, signed_up_at, trial_started_at,
                 phase_entered_at, created_via, verification_status,
                 verified_business_name, verification_method, gstin, verified_at)
            VALUES (%s, %s, 'onboarding', %s, %s, %s, %s, %s, %s, %s, 'web',
                    %s, %s, %s, %s, %s)
            ON CONFLICT (whatsapp_number) WHERE whatsapp_number IS NOT NULL
            DO NOTHING
            RETURNING id
            """,
            (business_name, plan_tier, whatsapp_number, preferred_language,
             business_type, city_tier, now, now, now,
             verification_status, verified_business_name, verification_method,
             verified_gstin, verified_at),
        ).fetchone()

        if row is None:
            existing = conn.execute(
                "SELECT id FROM tenants WHERE whatsapp_number = %s", (whatsapp_number,)
            ).fetchone()
            if existing is None:
                # Extreme race: the conflicting tenant was deleted between the INSERT
                # conflict and this SELECT. Raise a CLEAR error instead of crashing on a
                # None subscript (a cryptic AttributeError -> 500). (Pre-existing VT-82
                # hardening, surfaced by the VT-94 review.)
                raise RuntimeError(
                    "signup conflict but the conflicting tenant vanished (concurrent delete)"
                )
            tid = UUID(str(cast("dict[str, Any]", existing)["id"]))
            return SignupResult(tenant_id=tid, created=False, plan_tier=None, city_tier=None)

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
        # owner_name → business_profile (where get_business_profile reads it), merged
        # in the SAME txn (BYPASSRLS service_role; l1_entities one-per-tenant index).
        conn.execute(
            """
            INSERT INTO l1_entities (tenant_id, entity_type, attributes)
            VALUES (%s, 'business_profile', %s::jsonb)
            ON CONFLICT (tenant_id) WHERE entity_type = 'business_profile'
            DO UPDATE SET attributes = l1_entities.attributes || EXCLUDED.attributes
            """,
            (str(tid), json.dumps({"owner_name": owner_name})),
        )
        # CL-390 (review: kg_events outbox is durable + not DSR-purged): emit ONLY
        # the non-PII business_type (drives the KG CLASSIFIED_AS) — NEVER the
        # owner-provided business_name (subject data that would survive a DSR purge).
        emit_kg_event(conn, KgEventType.TENANT_CREATED, tid, {
            "business_type": business_type,
        })
        # VT-94: claim a founding slot as LATE as possible in the txn (shortest lock
        # hold). Atomic with the tenant create — a rolled-back signup never leaks a slot
        # (slots are never released, so a leak would be permanent). A claim upgrades the
        # tenant 'standard' -> 'founding'; at cap it stays 'standard'.
        if try_claim_founding_slot(conn, tid).claimed:
            conn.execute(
                "UPDATE tenants SET plan_tier = 'founding' WHERE id = %s", (str(tid),)
            )
            plan_tier = "founding"

    drain_kg_events(tid)
    return SignupResult(
        tenant_id=tid, created=True, plan_tier=plan_tier, city_tier=city_tier
    )


# --------------------------------------------------------------------------- #
# Endpoint orchestration: validate → create → city_tier → owner_name → welcome
# --------------------------------------------------------------------------- #

class SignupError(Exception):
    """Validation / conflict failure. ``code`` maps to an HTTP status at the route:
    'invalid_*' / 'consent' → 400; 'duplicate' → 409."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SignupGateError(SignupError):
    """VT-408 — the GSTIN hard-gate refused to create a tenant (verify-then-create).

    Distinct from a field/consent SignupError so the route can render the right owner-facing
    copy: a terminal REJECT (invalid/no GSTIN — generic "GST-registered businesses" screen,
    NO enumeration oracle) vs a retryable HOLD (vendor_down — "on our side, try again"). NO
    tenant is created on either. ``retryable`` drives reject-vs-hold UX; ``language`` selects
    the bilingual copy.
    """

    def __init__(self, *, outcome: str, retryable: bool, language: str) -> None:
        # code is the outcome tag (invalid_gstin | vendor_down); the route maps it to a status.
        super().__init__(outcome, f"signup gate: {outcome} (no tenant created)")
        self.outcome = outcome
        self.retryable = retryable
        self.language = language


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
    # VT-408: the GSTIN to verify BEFORE the tenant is created (verify-then-create). The
    # web signup form collects it as a gating sub-step (VT-406). The orchestrator gate is
    # the server-side enforcement seam — a missing/empty GSTIN is a hard reject (no GST =>
    # nothing). Defaults to '' so the field stays optional at the dataclass boundary, but
    # run_signup rejects an empty/unverified value (fail-closed).
    gstin: str = ""
    # VT-449: the MCA CIN the owner picked/resolved (registry leg). When present, run_signup fetches
    # MCA Company Master Data → uses the AUTHORITATIVE canonical name for the GST name-match (stronger
    # than the client-typed business_name) + persists tenant_mca_data (encrypted) post-create. Optional.
    cin: str = ""


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
        # Word-boundary match (review: unbounded substring false-positives on
        # legitimate names, e.g. a 3-letter blocked token inside a real word).
        if any(re.search(rf"\b{re.escape(term)}\b", folded) for term in blocked):
            raise SignupError("invalid_name", "name contains a disallowed term")


def _default_welcome(
    tenant_id: UUID, whatsapp_number: str, language: str,
    owner_name: str, trial_end: datetime,
) -> bool:
    """The real owner welcome-send seam (VT-393). Sends the Meta-approved
    ``team_welcome`` template (lang SID — EN/HI) to the owner's signup number via
    the owner-utility send seam, with {owner_name, trial_end_date}. Tests inject a
    recorder via ``welcome_send_fn``. NON-terminal: a failure here never rolls back
    the signup (the caller's try/except keeps a committed signup from 500-ing).

    VT-390 honesty invariant: ``welcome_sent`` (this return) is True ONLY on a
    confirmed send (``SendResult.success``). An unapproved SID → success=False /
    ``template_not_yet_approved`` → returns False + logged; never claims a delivery
    that did not happen. The recipient is the signup ``whatsapp_number`` (NOT
    tenants.owner_phone), so this is independent of the owner_phone-NULL blocker."""
    from orchestrator.owner_surface.owner_send import send_owner_template

    # VT-555: team_welcome4 is a strictly-transactional UTILITY quick-reply template — ONE variable
    # ({{1}} = owner name), NO trial/free wording, a "Complete Setup" button. ``trial_end`` is kept in
    # the signature for caller compatibility but is no longer sent (the copy dropped the trial date so
    # Meta classifies it UTILITY, not MARKETING). team_welcome3 was Meta-force-converted to MARKETING.
    result = send_owner_template(
        tenant_id,
        "team_welcome4",
        language,
        {"owner_name": owner_name},
        recipient_phone=whatsapp_number,
    )
    if not result.success:
        logger.warning(
            "signup: welcome NOT sent tenant=%s lang=%s (error_code=%s)",
            tenant_id, language, result.error_code,
        )
    else:
        logger.info(
            "signup: welcome sent tenant=%s lang=%s", tenant_id, language,
        )
    return result.success


def run_signup(
    inp: SignupInput,
    *,
    welcome_send_fn: Callable[..., bool] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    verify_search_fn: Callable[..., Any] | None = None,
) -> SignupOutcome:
    """The signup orchestration. Validate (→ SignupError on any field/consent) →
    **VT-408 GSTIN HARD-GATE (verify-then-create)** → ATOMIC create (tenant + consent +
    city_tier + owner_name in one txn; → SignupError 'duplicate' on a repeat whatsapp_number)
    → welcome send → auto-discovery kick → onboarding-journey start (all injectable / GUARDED +
    non-terminal — a raising kick never fails a committed signup).

    VT-408 (CL-442, Fazal 2026-06-24 — "a no-GST business doesn't get anything, neither paid
    nor trial"): the GSTIN is verified SERVER-SIDE *before* ``create_signup_tenant`` runs. No
    green verify ⇒ ``SignupGateError`` and NO tenant is created — no row, no consent, no
    founding slot, no burned whatsapp_number, and (critically) NONE of the welcome / discovery
    / journey product kicks fire (they all sit BELOW create, which never runs on a reject).
    ``vendor_down`` is a retryable HOLD (an outage must not turn a legit GST business away);
    ``invalid_gstin`` / missing GSTIN is a terminal REJECT. The verify is the ONLY door to a
    tenant — every kick below is therefore reachable ONLY on the verified path (the gate is
    upstream of all of them). ``verify_search_fn`` is the injectable GSTIN search seam for tests
    (no live creds)."""
    try:
        _validate(inp)
    except SignupError as exc:
        # VT-515: field/consent validation rejects are first-class failures. Emit before
        # re-raising so the viewer surfaces "consent not given" / "invalid phone" etc.
        _emit_signup_event(
            failure_type="validation",
            operation=exc.code,
            error=exc,
            severity="warning",
            impact="blocked_signup",
        )
        raise

    # VT-408 PRIMARY GATE — verify-then-create. Fail-closed: anything but a confirmed ACTIVE
    # GSTIN raises SignupGateError and creates NOTHING (so no product kick can fire below).
    from orchestrator.onboarding.entity_match import business_name_matches
    from orchestrator.onboarding.signup_gate import INVALID_GSTIN, verify_gstin_for_signup

    verify = verify_gstin_for_signup(inp.gstin, search_fn=verify_search_fn)
    if not verify.ok:
        # VT-515: gate rejection is already emitted by verify_gstin_for_signup (signup_gate.py).
        # Emit one more event at the signup-orchestration level so both the gate's component
        # ('verify') AND the create entry-point ('signup') appear in the debug log, giving the
        # viewer a complete chain for any rejection.
        _emit_signup_event(
            failure_type="vendor_error" if verify.retryable else "validation",
            operation=verify.outcome,
            error=f"signup gate refused tenant creation: {verify.outcome}",
            severity="warning" if verify.retryable else "error",
            impact=None if verify.retryable else "blocked_signup",
            vendor="sandbox" if verify.retryable else None,
        )
        raise SignupGateError(
            outcome=verify.outcome,
            retryable=verify.retryable,
            language=inp.preferred_language,
        )
    # VT-449: when the owner resolved a CIN, fetch MCA Company Master Data → use its AUTHORITATIVE
    # canonical name as the name-match anchor (registry-vs-registry — stronger than the client-typed
    # name, and it closes the impersonation-weak client-name gap). Best-effort: a vendor miss falls back
    # to the typed business_name (the VT-448 gate still holds). The MCA row is persisted post-create.
    mca_cmd = None
    # VT-449 PARKED (Fazal 2026-06-27): Sandbox MCA is unreliable (gov 504s) → gated OFF by default. When
    # off, NO company_master_data call fires; the name-match anchors on the confirmed business_name. Flip
    # ENABLE_SANDBOX_MCA on when a reliable provider lands (revertible — the code stays).
    from orchestrator.feature_flags import sandbox_mca_enabled

    if inp.cin.strip() and sandbox_mca_enabled():
        from orchestrator.integrations.methods.mca import company_master_data

        mca_cmd = company_master_data(inp.cin, reason=_MCA_SIGNUP_REASON)
    name_anchor = (
        mca_cmd.company_name if (mca_cmd and mca_cmd.ok and mca_cmd.company_name) else inp.business_name
    )
    # VT-448 NAME-MATCH SECURITY: a valid+active GSTIN earns a tenant ONLY if its authoritative registry
    # name plausibly matches the (MCA-canonical or owner-claimed) name. An unrelated-but-valid GSTIN (a
    # different business's registration) is REJECTED → the SAME generic invalid_gstin reject (no oracle).
    if not business_name_matches(name_anchor, verify.verified_name):
        _emit_signup_event(
            failure_type="validation",
            operation="name_mismatch_at_create",
            error="Verified GSTIN name does not match claimed business name — terminal reject (no oracle)",
            severity="error",
            impact="blocked_signup",
        )
        raise SignupGateError(outcome=INVALID_GSTIN, retryable=False, language=inp.preferred_language)

    now = (now_fn or _utcnow)()
    _now = now_fn or (lambda: now)  # single instant for both create + trial_end

    res = create_signup_tenant(
        business_name=inp.business_name,
        owner_name=inp.owner_name,
        whatsapp_number=inp.whatsapp_number,
        preferred_language=inp.preferred_language,
        city=inp.city,
        business_type=inp.business_type,
        consent_dpdpa=inp.consent_dpdpa,
        consent_residency=inp.consent_residency,
        verified_gstin=verify.gstin,
        verified_business_name=verify.verified_name,
        now_fn=_now,
    )
    if not res.created:
        # VT-515: duplicate registration is a first-class failure — the viewer should surface
        # it so ops can investigate ownership/support cases. tenant_id is the EXISTING tenant
        # (the one that already owns this whatsapp_number).
        _emit_signup_event(
            failure_type="validation",
            operation="duplicate_whatsapp",
            error="whatsapp_number already registered — duplicate create blocked",
            severity="warning",
            impact="blocked_signup",
            tenant_id=res.tenant_id,
        )
        raise SignupError("duplicate", "this whatsapp_number is already registered")

    # VT-406 reconciliation (verify-then-create completion): persist the verified entity as the
    # discovery anchor on the NEW tenant. Part A's confirm_and_verify was tenant-scoped/pre-create (it
    # no-ops on the empty pre-create tenant_id); the anchor truly lands HERE, post-create, with the real
    # tenant_id and the GATE's SERVER-verified gstin/name — NEVER a client-supplied value (IDOR rule).
    # Best-effort + non-terminal, like the kicks below.
    if verify.gstin:
        try:
            from orchestrator.onboarding.entity_match import persist_entity_anchor

            persist_entity_anchor(res.tenant_id, gstin=verify.gstin, verified_name=verify.verified_name)
        except Exception:  # noqa: BLE001 — anchor is best-effort; never fail a committed signup
            logger.exception("signup: entity anchor persist failed tenant=%s (non-terminal)", res.tenant_id)

    # VT-449: persist the MCA company data (encrypted PII via mca_store) on the new tenant — best-effort,
    # non-terminal, like the anchor + the kicks below. Counts-only logging; no PII reaches a log line.
    if mca_cmd and mca_cmd.ok:
        try:
            from orchestrator.onboarding.mca_store import store_company_master_data

            store_company_master_data(res.tenant_id, mca_cmd)
        except Exception:  # noqa: BLE001 — MCA store is best-effort; never fail a committed signup
            logger.exception("signup: MCA company store failed tenant=%s (non-terminal)", res.tenant_id)

    # Welcome send: GUARDED + non-terminal. The tenant is already committed; a send (or the
    # trial.yaml read) failure must NOT 500 the signup — log + report welcome_sent=False.
    sent = False
    try:
        trial_end = now + timedelta(days=_trial_days())
        sent = bool((welcome_send_fn or _default_welcome)(
            res.tenant_id, inp.whatsapp_number, inp.preferred_language,
            inp.owner_name, trial_end,
        ))
        if not sent:
            # VT-519: the welcome is the owner's FIRST signal that signup worked. A
            # non-delivery (unapproved SID, transport refusal, sandbox-not-joined, etc.)
            # was previously SILENT — `_default_welcome` only `logger.warning`s, so the
            # owner gets silence and the VT-515 feed shows NOTHING (the exact failure
            # Fazal hit: "onboarding completed but no welcome arrived"). Emit a first-class
            # debug_event so a missing welcome is always observable + diagnosable.
            _emit_signup_event(
                failure_type="vendor_error",
                operation="welcome_not_delivered",
                error="welcome send returned not-delivered (no confirmed SendResult.success)",
                severity="error",
                impact="owner_no_welcome",
                tenant_id=res.tenant_id,
                vendor="twilio",
            )
    except Exception as exc:  # noqa: BLE001 — welcome is best-effort; never fail the signup
        logger.exception("signup: welcome send failed tenant=%s (non-terminal)", res.tenant_id)
        # VT-519: a raising welcome send was also silent in the feed — emit it too.
        _emit_signup_event(
            failure_type="exception",
            operation="welcome_send_raised",
            error=exc,
            severity="error",
            impact="owner_no_welcome",
            tenant_id=res.tenant_id,
            vendor="twilio",
        )

    # VT-366: kick the Auto-Discovery Engine — post-commit, NON-BLOCKING (DBOS bg workflow), exactly
    # like the welcome. The tenant is already committed; the engine assembles a DRAFT profile from
    # public sources (owner-confirmed in onboarding, NEVER asserted as fact). A kick failure must NOT
    # 500 the signup. Skipped cleanly if DBOS isn't launched (tests / non-workflow contexts).
    try:
        from dbos import DBOS

        from orchestrator.onboarding.auto_discovery import auto_discovery_workflow

        DBOS.start_workflow(
            auto_discovery_workflow,
            str(res.tenant_id),
            {
                # VT-406 reconciliation: anchor discovery on the SERVER-VERIFIED entity, not the raw
                # typed name — so the draft anchors on the confirmed entity (the Sundaram wrong-anchor
                # fix) and VT-407's discover_gst keys off the verified GSTIN.
                "business_name": verify.verified_name or inp.business_name,
                "gstin": verify.gstin,
                "business_type": inp.business_type,
                "city": inp.city,
                "whatsapp_number": inp.whatsapp_number,
            },
        )
    except Exception:  # noqa: BLE001 — discovery is best-effort; never fail the signup
        logger.exception("signup: auto-discovery kick failed tenant=%s (non-terminal)", res.tenant_id)

    # VT-367: start the onboarding JOURNEY here (pending — empty queue; the async discovery fills it,
    # and the owner's first inbound asks the first question). Starting it at signup (NOT lazily on an
    # arbitrary inbound) is what makes the owner's first message route to the journey, never the cold
    # brain, WITHOUT intercepting inbound for non-onboarding tenants (they have no journey row).
    # Best-effort: a start failure must NOT 500 the signup (the owner just falls to the normal flow).
    try:
        from orchestrator.onboarding.journey import start_journey

        start_journey(res.tenant_id, [])
    except Exception:  # noqa: BLE001 — non-terminal; never fail the signup
        logger.exception("signup: onboarding-journey start failed tenant=%s (non-terminal)", res.tenant_id)

    return SignupOutcome(
        tenant_id=res.tenant_id,
        plan_tier=cast(str, res.plan_tier),
        city_tier=cast(str, res.city_tier),
        welcome_sent=sent,
    )


# ---------------------------------------------------------------------------
# VT-515: debug event helper for the signup orchestration leg
# ---------------------------------------------------------------------------

def _emit_signup_event(
    *,
    failure_type: str,
    operation: str,
    error: BaseException | str,
    severity: str = "error",
    impact: str | None = None,
    tenant_id: UUID | None = None,
    vendor: str | None = None,
) -> None:
    """Emit a debug_event for a signup-path failure. Fail-soft — never raises."""
    try:
        from orchestrator.observability.debug_log import emit_debug_event

        emit_debug_event(
            failure_type=failure_type,
            component="signup",
            operation=operation,
            error=error,
            severity=severity,
            impact=impact,
            tenant_id=tenant_id,
            vendor=vendor,
        )
    except Exception:  # noqa: BLE001 — never raise into the signup flow
        pass


__all__ = [
    "SignupError",
    "SignupGateError",
    "SignupInput",
    "SignupOutcome",
    "SignupResult",
    "create_signup_tenant",
    "run_signup",
    "valid_business_types",
]
