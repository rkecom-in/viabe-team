"""Twilio inbound ingress endpoint (VT-3.3a/b + VT-3.3a-fix-1).

Deterministic ingress ONLY (Pillar 1): verify the internal secret, reject a
malformed payload, resolve the tenant, rate-limit, then start the durable
webhook workflow. No reasoning, no classification.

Dedup detection and event construction live INSIDE webhook_pipeline_run (the
durable boundary) — see PR-fix-1 / CL-72. This handler only validates and
starts the workflow.
"""

from __future__ import annotations

import hmac
import logging
import os
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from dbos import DBOS, SetWorkflowID
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from orchestrator.error_router import route_failure
from orchestrator.failures import FailureRecord, FailureType
from orchestrator.graph import get_pool
from orchestrator.integrations.customer_inbound import customer_inbound_run
from orchestrator.onboarding.whatsapp_signup import whatsapp_signup_run
from orchestrator.privacy.consent import record_consent
from orchestrator.runner import webhook_pipeline_run
from orchestrator.utils.phone_token import hash_phone

logger = logging.getLogger(__name__)
router = APIRouter()

# Fixed-window rate limits (per minute).
_PER_TENANT_LIMIT = 30
_WORKSPACE_LIMIT = 500
# All-zeros sentinel tenant_id for the workspace-wide bucket (see migration 013).
_WORKSPACE_SENTINEL = "00000000-0000-0000-0000-000000000000"
# VT-691 — dedicated sentinel bucket for unknown-sender SIGNUP prompts (any number on earth can
# hit this path, so it gets its own, much tighter workspace-wide budget than owner traffic).
_SIGNUP_SENTINEL = "00000000-0000-0000-0000-000000000001"
_SIGNUP_WORKSPACE_LIMIT = 10  # consent-flow starts per minute, workspace-wide


class TwilioIngressBody(BaseModel):
    """Request body forwarded by team-web (VT-3.3b) — raw Twilio fields only."""

    twilio_fields: dict[str, Any]


# VT-567 (live-drill root cause, 2026-07-02): a REAL WhatsApp inbound arrives channel-prefixed —
# From='whatsapp:+91…' / To='whatsapp:+91…' — while every downstream identity (tenants.
# whatsapp_number, phone tokenisation, consent/opt-out lookups) keys on PLAIN E.164. Nothing in
# the chain stripped the prefix, so _lookup_tenant missed every live inbound → reason=
# 'unknown_sender' → 200-and-drop: the owner's COMPLETE_SETUP tap never started a run (tests had
# only ever posted plain E.164). Normalize ONCE, here at the ingress boundary, for BOTH the
# lookups and the fields handed to the workflow — a partial strip would fork the phone-token
# space (hash_phone('whatsapp:+91…') != hash_phone('+91…')).
_WA_CHANNEL_PREFIX = "whatsapp:"
_WA_PHONE_FIELDS = ("From", "To", "WaId")


def _strip_wa_prefix(value: str) -> str:
    return value[len(_WA_CHANNEL_PREFIX):] if value.startswith(_WA_CHANNEL_PREFIX) else value


def _normalize_wa_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Return ``fields`` with the WhatsApp channel prefix stripped off phone-bearing keys.
    Plain-E.164 payloads (tests, SMS) pass through byte-identical."""
    out = dict(fields)
    for key in _WA_PHONE_FIELDS:
        v = out.get(key)
        if isinstance(v, str) and v.startswith(_WA_CHANNEL_PREFIX):
            out[key] = _strip_wa_prefix(v)
    return out


# VT-582 (CL-2026-07-03-conversing-surfaces-and-harness): the DEV-ONLY ingress secret the
# server-side conversation harness (canaries/convo_harness.py) drives the DEPLOYED dev orchestrator
# with, so a fresh operator session can hold a full WhatsApp conversation without ever minting the
# real INTERNAL_API_SECRET. Accepted ONLY on a POSITIVELY-dev env (EXPECTED_ENV in {dev,development},
# the VT-362 sentinel mirrored from auth/prod_safety). On prod — or an unset/garbage EXPECTED_ENV —
# it is IGNORED, fail-closed: the CL-431 prod gate, so DEV_TEST_INGRESS_SECRET can NEVER authenticate
# a request in production. The prod INTERNAL_API_SECRET path is unchanged on every env.
_DEV_ENV_VALUES = frozenset({"dev", "development"})


def _dev_ingress_enabled() -> bool:
    """True only when EXPECTED_ENV POSITIVELY reads a non-prod dev value (VT-362 sentinel).

    Fail-closed exactly like ``auth/prod_safety._is_prod``: an unset/unknown/garbage EXPECTED_ENV
    (including ``prod``) returns False, so the dev ingress secret is inert off dev."""
    return os.environ.get("EXPECTED_ENV", "").strip().lower() in _DEV_ENV_VALUES


def _verify_internal_secret(provided: str | None) -> bool:
    """Constant-time compare against the accepted ingress secret(s) (Pillar 8 — no bespoke crypto).

    Accepts the prod INTERNAL_API_SECRET on EVERY env; ADDITIONALLY — only on a positively-dev env
    (VT-582) — the harness's DEV_TEST_INGRESS_SECRET. Both compares are constant-time
    (``hmac.compare_digest``); neither secret is ever logged. A request that authenticates via the
    dev secret is otherwise IDENTICAL downstream (same payload validation, tenant resolve, rate
    limits) — this only widens WHO may open the door on dev, nothing beneath it."""
    if not provided:
        return False
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if expected and hmac.compare_digest(provided, expected):
        return True
    if _dev_ingress_enabled():
        dev_secret = os.environ.get("DEV_TEST_INGRESS_SECRET", "")
        if dev_secret and hmac.compare_digest(provided, dev_secret):
            return True
    return False


def _lookup_tenant(from_phone: str) -> str | None:
    """Resolve a tenant by WhatsApp number. FAIL-CLOSED on ambiguity (VT-416 PR-3).

    Returns the single matching tenant id, or ``None`` when the number matches
    no tenant. If MORE THAN ONE tenant ever shares the number, this does NOT
    silently pick the newest (the old ``ORDER BY created_at DESC LIMIT 1``
    behaviour, which could cross-route a customer's inbound to the wrong owner) —
    it logs a clear error and returns ``None``, routing the message to the
    existing unmatched path rather than to a guessed owner.

    The canonical guarantee is the DB constraint: migration 066
    (``tenants_whatsapp_number_key``, a partial UNIQUE index on
    ``whatsapp_number WHERE whatsapp_number IS NOT NULL``, VT-267 / Fazal D1
    2026-06-02) makes the business WhatsApp number a globally-unique tenant
    identity, so a duplicate cannot be inserted in the first place. This code
    guard is DEFENCE-IN-DEPTH: it converts the (now schema-impossible) two-match
    case from a silent newest-wins mis-route into a fail-closed, logged
    non-match — surviving a hypothetical future regression that drops the
    constraint, without ever cross-routing a customer to the wrong owner.
    """
    if not from_phone:
        return None
    with get_pool().connection() as conn:
        # Fetch up to two rows: one is the happy path, two proves ambiguity.
        rows = conn.execute(
            "SELECT id FROM tenants WHERE whatsapp_number = %s LIMIT 2",
            (from_phone,),
        ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        logger.error(
            "twilio-ingress: ambiguous whatsapp_number — %d tenants share number=%s; "
            "fail-closed (routing to unmatched path, NOT guessing an owner). "
            "This should be impossible under the tenants_whatsapp_number_key "
            "UNIQUE index (mig 066) — investigate the schema if it fires.",
            len(rows),
            hash_phone(from_phone),
        )
        return None
    return str(rows[0]["id"])


def _lookup_customer_inbound_tenant(to_phone: str) -> str | None:
    """VT-287: resolve the tenant whose LIVE WABA number is ``to_phone``.

    Customer-inbound (inbound-first) is the inverse of owner-inbound: the message is
    addressed TO the business's WABA number (`To`), FROM a customer. Only `live` WABAs
    are eligible (a tenant can't receive customer traffic pre-verification). Service
    pool (this resolution has no tenant context yet)."""
    if not to_phone:
        return None
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT tenant_id FROM tenant_whatsapp_accounts "
            "WHERE phone_number = %s AND status = 'live' LIMIT 1",
            (to_phone,),
        ).fetchone()
    return str(row["tenant_id"]) if row else None


# DBOS workflow statuses for a prior workflow that is still progressing — the
# rest (ERROR / CANCELLED / MAX_RECOVERY_ATTEMPTS_EXCEEDED) are terminally dead.
# Verified live against dbos 2.x WorkflowStatusString (CL-96).
_RECOVERING_STATUSES = frozenset({"PENDING", "ENQUEUED", "DELAYED"})


def _ingress_reason(prior: Any) -> str:
    """Classify a (re)delivered MessageSid by the prior workflow's DBOS status.

    ``DBOS.start_workflow`` no-ops on a known workflow_id (idempotency) — this
    only *reports* the prior workflow's state, it never re-triggers it:

    - None    -> ``started``  — brand-new MessageSid.
    - SUCCESS -> ``dupe``     — true Twilio retry of an already-handled message.
    - PENDING / ENQUEUED / DELAYED -> ``recovering`` — workflow still in flight;
      DBOS recovery / the queue will carry it to completion.
    - ERROR / CANCELLED / MAX_RECOVERY_ATTEMPTS_EXCEEDED -> ``terminal_failure``
      — the prior workflow is dead and ``start_workflow`` no-ops, so nothing
      recovers it (Pillar 7 — do not report a dead run as ``recovering``).
    """
    if prior is None:
        return "started"
    if prior.status == "SUCCESS":
        return "dupe"
    if prior.status in _RECOVERING_STATUSES:
        return "recovering"
    return "terminal_failure"


def _bump_bucket(conn: Any, tenant_id: str, limit: int) -> bool:
    """Atomically increment the current minute bucket. Return True if within limit."""
    row = conn.execute(
        "INSERT INTO rate_limit_buckets (tenant_id, window_start, count) "
        "VALUES (%s, date_trunc('minute', now()), 1) "
        "ON CONFLICT (tenant_id, window_start) "
        "DO UPDATE SET count = rate_limit_buckets.count + 1 "
        "RETURNING count",
        (tenant_id,),
    ).fetchone()
    return row["count"] <= limit


def _within_rate_limits(tenant_id: str) -> bool:
    """Check per-tenant (30/min) and workspace (500/min) inbound rate limits."""
    with get_pool().connection() as conn:
        if not _bump_bucket(conn, tenant_id, _PER_TENANT_LIMIT):
            return False
        return _bump_bucket(conn, _WORKSPACE_SENTINEL, _WORKSPACE_LIMIT)


def _within_signup_rate_limit() -> bool:
    """VT-691 — the unknown-sender signup budget (10/min workspace-wide, own sentinel bucket).
    Per-number cooldown/idempotency lives in whatsapp_signup_sessions; this is the coarse
    transport-level flood valve. Fail-CLOSED: an unreadable bucket refuses the prompt (an
    unknown number never has a delivery guarantee to protect)."""
    try:
        with get_pool().connection() as conn:
            return _bump_bucket(conn, _SIGNUP_SENTINEL, _SIGNUP_WORKSPACE_LIMIT)
    except Exception:  # noqa: BLE001 — fail-closed on the abuse valve
        logger.warning("twilio-ingress: signup rate-bucket read failed (fail-closed)")
        return False


@router.post("/api/orchestrator/twilio-ingress")
def twilio_ingress(
    body: TwilioIngressBody,
    x_internal_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Validate, resolve the tenant, rate-limit, and start the webhook workflow.

    Returns ``{workflow_id, reason}`` — reason is one of: started, dupe,
    recovering, terminal_failure, unknown_sender, rate_limit_exceeded,
    error_logged. 403 on a bad secret;
    400 on a malformed payload (missing MessageSid). After validation, never
    5xx for an application error (Pillar 7).
    """
    if not _verify_internal_secret(x_internal_secret):
        # VT-29: classify as a business failure so the taxonomy stays the sole
        # surface for routing decisions. Tenant is unknown here (the request
        # never authenticates), so route_failure logs without persisting and
        # returns ACCEPT_AND_LOG. The 403 still ships unchanged.
        route_failure(
            FailureRecord(
                failure_type=FailureType.WEBHOOK_SIGNATURE_FAILURE,
                message="invalid internal secret on twilio-ingress",
                occurred_at=datetime.now(UTC),
            )
        )
        raise HTTPException(status_code=403, detail="invalid internal secret")

    # C3 fix (CL-73): reject a malformed payload before any side-effects.
    # Twilio always sends a MessageSid; a missing one is a team-web forwarder
    # bug — surface it so team-web can log/alert rather than collapsing every
    # malformed request into one workflow_id.
    # VT-567: normalize the WhatsApp channel prefix ONCE at the boundary — lookups AND the
    # workflow both see plain E.164 (see _normalize_wa_fields).
    fields = _normalize_wa_fields(body.twilio_fields)
    message_sid = str(fields.get("MessageSid", ""))
    if not message_sid:
        raise HTTPException(status_code=400, detail="missing MessageSid")

    from_phone = str(fields.get("From", ""))
    to_phone = str(fields.get("To", ""))
    try:
        tenant_id = _lookup_tenant(from_phone)
        if tenant_id is None:
            # VT-287: not an owner inbound — try the customer-inbound path (message
            # addressed TO a tenant's live WABA number). Deterministic, separate from
            # the owner pipeline (Cowork ruling 2026-06-02).
            customer_tenant = _lookup_customer_inbound_tenant(to_phone)
            if customer_tenant is not None and _within_rate_limits(customer_tenant):
                workflow_id = f"wa_customer_{message_sid}"
                prior = DBOS.get_workflow_status(workflow_id)
                with SetWorkflowID(workflow_id):
                    DBOS.start_workflow(
                        customer_inbound_run,
                        customer_tenant,
                        from_phone,
                        str(fields.get("Body", "")),
                    )
                return {"workflow_id": workflow_id, "reason": _ingress_reason(prior)}
            # VT-691 — WhatsApp-initiated signup: an unknown sender BECOMES a signup, behind
            # ENABLE_WHATSAPP_SIGNUP (default OFF → the unknown_sender fall-through below is
            # byte-identical to today). Transport-only here (Pillar 1): flag + rate gate, then
            # hand to the durable workflow — consent CLASSIFICATION never runs in this endpoint.
            # SetWorkflowID(wa_signup_{sid}) makes a Twilio redelivery a no-op replay.
            # ``customer_tenant is None`` is LOAD-BEARING (adversarial-verify finding B): a
            # message addressed TO a live business WABA is that business's CUSTOMER — when the
            # branch above declines it (e.g. the tenant is rate-limited), it must fall through
            # to the silent drop, NEVER into a Viabe signup solicitation of a third party's
            # customer (whose new tenant would then cross-route their future messages).
            from orchestrator.feature_flags import whatsapp_signup_enabled

            if whatsapp_signup_enabled() and from_phone and customer_tenant is None:
                if _within_signup_rate_limit():
                    # whatsapp_signup_run is imported at module top (register-before-launch —
                    # the customer_inbound_run pattern; a lazily-registered workflow would land
                    # after app_version is hashed, the VT-464 recovery hazard).
                    # VT-697 — typing feedback for the signup path too (consent card compose).
                    try:
                        from orchestrator.utils.twilio_send import send_typing_indicator

                        send_typing_indicator(message_sid)
                    except Exception:  # noqa: BLE001
                        pass
                    workflow_id = f"wa_signup_{message_sid}"
                    prior = DBOS.get_workflow_status(workflow_id)
                    with SetWorkflowID(workflow_id):
                        DBOS.start_workflow(
                            whatsapp_signup_run,
                            from_phone,
                            str(fields.get("Body", "")),
                            message_sid,
                        )
                    logger.info(
                        "twilio-ingress: whatsapp_signup from=%s sid=%s",
                        hash_phone(from_phone), message_sid,
                    )
                    return {"workflow_id": workflow_id, "reason": _ingress_reason(prior)}
                logger.warning(
                    "twilio-ingress: whatsapp_signup rate_limit_exceeded sid=%s", message_sid
                )
                return {"workflow_id": None, "reason": "rate_limit_exceeded"}
            logger.info(
                "twilio-ingress: unknown_sender from=%s sid=%s",
                hash_phone(from_phone) if from_phone else "<empty>",
                message_sid,
            )
            return {"workflow_id": None, "reason": "unknown_sender"}

        if not _within_rate_limits(tenant_id):
            logger.warning(
                "twilio-ingress: rate_limit_exceeded tenant=%s sid=%s",
                tenant_id,
                message_sid,
            )
            return {"workflow_id": None, "reason": "rate_limit_exceeded"}

        # VT-697 — read-tick + "typing…" the moment an OWNER inbound lands (fire-and-forget
        # daemon thread; zero hot-path latency, fail-soft). Transport-only feedback — no
        # classification here (Pillar 1). Clears when our reply delivers (or 25s).
        try:
            from orchestrator.utils.twilio_send import send_typing_indicator

            send_typing_indicator(message_sid)
        except Exception:  # noqa: BLE001 — presentation only, never blocks ingress
            pass

        workflow_id = f"twilio_inbound_{message_sid}"
        run_id = str(uuid5(NAMESPACE_URL, message_sid))
        # Read-only pre-check (no side-effect): the prior workflow's DBOS status
        # for this MessageSid, if any. Dedup itself happens inside the workflow.
        prior = DBOS.get_workflow_status(workflow_id)
        with SetWorkflowID(workflow_id):
            DBOS.start_workflow(webhook_pipeline_run, tenant_id, run_id, fields)
        return {
            "workflow_id": workflow_id,
            "reason": _ingress_reason(prior),
        }
    except Exception:
        # Pillar 7: never 5xx for an application error.
        logger.exception("twilio-ingress: failed sid=%s", message_sid)
        return {"workflow_id": None, "reason": "error_logged"}


# --- VT-598 addendum: dev-test consent seeding ---------------------------------------------------
#
# LIVE FINDING (2026-07): canaries/convo_harness.py's --seed-lapsed-customers writes
# record_of_consent by calling orchestrator.privacy.consent.record_consent() DIRECTLY in the
# harness's OWN process (via `railway run`, which does not inject the SEALED
# TEAM_PHONE_HASH_SALT). That tokenises phone_e164 with a throwaway/default salt, while the
# DEPLOYED service (this same module, running for real) computes phone_token with its real sealed
# salt. The two tokens never match, so a locally-seeded consent row can never join against what the
# deployed sales_recovery detection query (db/wrappers._LAPSED_CANDIDATES_SQL) computes server-side
# — a seeded cohort silently reads as empty (proven live: 6 seeded lapsed customers -> "dormant
# cohort count is 0"). The existing /api/orchestrator/consent/capture endpoint calls record_consent
# SERVER-SIDE already (the correct fix shape) but is guarded by INTERNAL_API_SECRET only, which is
# SEALED on Railway exactly like TEAM_PHONE_HASH_SALT — the harness (which only has
# DEV_TEST_INGRESS_SECRET injected) cannot authenticate to it either.
#
# Fix: a DEV-ONLY sibling that does the SAME server-side record_consent() call, guarded by the
# SAME _verify_internal_secret() this module already uses for /twilio-ingress — reused verbatim,
# not reimplemented, so the CL-431 fail-closed prod gate (DEV_TEST_INGRESS_SECRET is INERT unless
# EXPECTED_ENV positively reads dev/development) covers this endpoint identically. Note the prod
# INTERNAL_API_SECRET ALSO authenticates here on every env, same as /consent/capture — this does
# NOT add a new prod capability: whoever holds that secret can already write an arbitrary consent
# record via /consent/capture today, so exposing the same record_consent() call under a second path
# does not widen what a real INTERNAL_API_SECRET holder can already do in prod.


class ConsentSeedBody(BaseModel):
    tenant_id: str
    phone_e164: str
    consent_text_version: str


class ConsentSeedResponse(BaseModel):
    recorded: bool
    active: bool
    # First 12 chars only — this is a dev-test convenience echo (log/debug correlation), not a
    # capability the caller needs the full token for; no reason to put more of it on the wire.
    phone_token_prefix: str


def _parse_dev_test_tenant(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid tenant_id") from exc


@router.post("/api/orchestrator/dev-test/consent-seed")
def dev_test_consent_seed(
    body: ConsentSeedBody,
    x_internal_secret: str | None = Header(default=None),
) -> ConsentSeedResponse:
    """VT-598 addendum — DEV-ONLY consent seeding for the conversation harness's
    ``--seed-lapsed-customers`` path (see the module-level note above for the full finding).

    Guarded by ``_verify_internal_secret`` — the SAME function ``/twilio-ingress`` uses, reused
    verbatim: accepts the prod ``INTERNAL_API_SECRET`` on every env, ADDITIONALLY accepts
    ``DEV_TEST_INGRESS_SECRET`` only on a positively-dev ``EXPECTED_ENV`` (CL-431 fail-closed — the
    dev secret is inert on prod). Calls ``record_consent`` IN THIS PROCESS — i.e. with the deployed
    service's own (sealed) ``TEAM_PHONE_HASH_SALT`` — so the ``phone_token`` this writes is
    BYTE-IDENTICAL to what this same service computes for the same ``phone_e164`` anywhere else
    (the detection query included). This is the entire fix: move the tokenisation into the process
    that actually holds the real salt.

    TIGHTER THAN /consent/capture (review decision 2026-07-04): this route refuses EVERYTHING —
    including the prod ``INTERNAL_API_SECRET`` — unless ``EXPECTED_ENV`` positively reads dev
    (``_dev_ingress_enabled``). A dev-test seeding surface has zero legitimate prod use, so off-dev
    it answers 404 (indistinguishable from route-absent) rather than existing as a
    capability-equivalent twin of /consent/capture.
    """
    if not _dev_ingress_enabled():
        raise HTTPException(status_code=404, detail="not found")
    if not _verify_internal_secret(x_internal_secret):
        raise HTTPException(status_code=403, detail="invalid internal secret")
    tenant_id = _parse_dev_test_tenant(body.tenant_id)
    rec = record_consent(
        tenant_id,
        body.phone_e164,
        consent_text_version=body.consent_text_version,
        consent_method="dev_test_seed",
        source="convo-harness-seed",
    )
    return ConsentSeedResponse(
        recorded=True,
        active=rec.active,
        phone_token_prefix=rec.phone_token[:12],
    )
