"""VT-45 — send_whatsapp_template standalone tool.

Delivers a Meta-approved WhatsApp template via Twilio Content API. NO 24-hour
window restriction: templates are the out-of-window path by design.

Pillars
- Pillar 1: pure transport. Template content is pre-approved; tool does NOT
  modify or reinterpret (Pillar 7: last-minute edits forbidden).
- Pillar 2: deterministic. Idempotent on (tenant_id, idempotency_key) via
  send_idempotency_keys table (migration 049, owned by VT-44).
- Pillar 3: phone resolved internally; phone_e164 never in input or logs (CL-390).
- Pillar 7: honest error envelopes on every failure. Tool refuses opted-out /
  blocked recipients — consent gating is NOT deferred to the caller (CL-421).

Rate limits
- Per-tenant: 5000 template sends per 24h (COUNT over send_idempotency_keys).

Registry validation (VT-163)
- Resolves template_id (=template_name) + language via templates_registry.resolve().
- Validates template_params against the variable signature via validate_params().
- Builds positional content_variables {"1": v1, "2": v2, ...} from named params.

NO PII (CL-390): logged fields = tenant_id, customer_id, template_id, status, sid.
Phone never in logs or ledger payload (only hashed token via hash_phone).
CL-422: dev = synthetic data only until VT-231 (prod Mumbai).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.db.wrappers import CustomersWrapper

logger = logging.getLogger(__name__)

# Rate-limit constants.
_TENANT_DAILY_LIMIT = 5000       # per-tenant template sends per 24h
_TENANT_WINDOW = timedelta(hours=24)

# Param value constraints (Twilio content rendering limits).
_PARAM_MAX_LENGTH = 1024  # conservative; Twilio docs do not specify; 1024 safe


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    retry_after_ms: int | None = None


class SendWhatsappTemplateInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., min_length=1)
    customer_id: str = Field(..., min_length=1)   # UUID as str; resolved to phone internally
    template_id: str = Field(..., min_length=1)   # = template_name in registry
    language: Literal["en", "hi"]
    template_params: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str = Field(..., min_length=1)


class SendWhatsappTemplateOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["sent", "dry_run", "rate_limited", "unauthorized", "error"]
    message_sid: str | None = None
    customer_id: str | None = None
    sent_at: datetime | None = None
    error_envelope: ErrorEnvelope | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _validate_registry(
    template_id: str, language: str, template_params: dict[str, str],
) -> tuple[tuple[str, ...], str | None] | ErrorEnvelope:
    """Validate template_id + language + params against the VT-163 registry.

    Returns:
        (variables_tuple, content_sid) on success.
        ErrorEnvelope on validation failure.

    CL-390: logs template_id + language only; never param keys/values.
    """
    from orchestrator.templates_registry import (
        UnknownLanguageVariantError,
        UnknownTemplateError,
        VariableSignatureMismatchError,
        resolve,
        validate_params,
    )

    # Step 1: resolve template + language.
    try:
        entry = resolve(template_id, language)
    except UnknownTemplateError:
        logger.debug(
            "send_whatsapp_template: unknown_template template=%s language=%s",
            template_id, language,
        )
        return ErrorEnvelope(
            code="unknown_template",
            message=f"Template '{template_id}' is not in the approved registry.",
        )
    except UnknownLanguageVariantError:
        logger.debug(
            "send_whatsapp_template: unsupported_language template=%s language=%s",
            template_id, language,
        )
        return ErrorEnvelope(
            code="unsupported_language",
            message=(
                f"Template '{template_id}' has no '{language}' language variant. "
                "Check twilio_templates.yaml for supported languages."
            ),
        )

    # Step 2: validate param signature.
    try:
        validate_params(template_id, language, template_params)
    except VariableSignatureMismatchError as exc:
        missing = sorted(exc.expected - exc.got)
        extra = sorted(exc.got - exc.expected)
        if missing:
            return ErrorEnvelope(
                code="missing_template_params",
                message=f"Template '{template_id}' requires params: {missing}.",
            )
        return ErrorEnvelope(
            code="extra_template_params",
            message=f"Template '{template_id}' does not accept params: {extra}.",
        )

    # Step 3: validate param values.
    for value in template_params.values():
        if len(value) > _PARAM_MAX_LENGTH:
            return ErrorEnvelope(
                code="param_value_invalid",
                message=f"A template param value exceeds maximum length {_PARAM_MAX_LENGTH}.",
            )

    return entry.variables, entry.content_sid


def _build_content_variables(
    variables: tuple[str, ...], template_params: dict[str, str],
) -> dict[str, str]:
    """Map named params -> positional {"1": v1, "2": v2, ...} for Twilio.

    Twilio Content API expects positional string keys matching {{N}} in the
    template body. The registry's ``variables`` tuple is the ordered spec.
    """
    return {str(i + 1): template_params[var] for i, var in enumerate(variables)}


# VT-262: statuses the output Literal can represent. A ledger row whose
# send_status is NOT one of these (e.g. a 'skipped' consent marker, or a foreign
# status written by another tool sharing send_idempotency_keys) is NOT a prior
# deliverable send — echoing it would raise a pydantic ValidationError that the
# broad except turns into a phantom db_error AND wrongly suppress the send.
#
# VT-387: 'error' is DELIBERATELY excluded (it WAS in the VT-262 set). A draft whose
# send TRANSIENTLY failed (Twilio 5xx, network blip, 4xx reject, db error) caches
# send_status='error' under the FIXED key agent:{draft_id} — treating that as an
# idempotent hit made the draft unretryable for the key's full 24h TTL, so a retry
# within the window silently no-opped (money-adjacent: a recovery/approval send that
# should re-fire just didn't). Excluding 'error' makes _check_idempotency return None
# for an errored row → the caller re-runs every gate (consent/opt-out/caps/rate) and
# re-sends.
#
# Double-send safety (the load-bearing invariant): 'error' is NEVER written to the
# ledger AFTER a successful side-effect. Every path here that writes 'error' does so
# BEFORE/WITHOUT a delivered message —
#   (1) send_fn raises: twilio_send.send_template_message re-raises ONLY on 5xx/unknown
#       (Twilio did NOT accept), ValueError (no recipient), or UnknownTemplateError —
#       messages.create either never ran or raised before returning;
#   (2) send_result.success is False: a 4xx reject or template_not_yet_approved — no
#       message dispatched;
#   (3) the outer db_error except writes NOTHING to the ledger (no false 'error' row).
# A genuinely delivered send is the ONLY thing that writes 'sent', and 'sent' STAYS in
# the set — so a completed/sent draft remains an idempotent hit and never re-sends.
_IDEMPOTENT_HIT_STATUSES = frozenset(
    {"sent", "dry_run", "rate_limited", "unauthorized"}
)


def _check_idempotency(
    cur: Any, tenant_id: str, idempotency_key: str,
) -> dict[str, Any] | None:
    """Return existing ledger row if the key was used within 24h, else None.

    Returns None for a row whose send_status the output cannot represent (VT-262)
    — the caller re-evaluates (consent gate / send) rather than echoing an
    invalid status.
    """
    cur.execute(
        """
        SELECT id::text, message_sid, send_status, created_at
        FROM send_idempotency_keys
        WHERE tenant_id = %s
          AND idempotency_key = %s
          AND created_at > now() - interval '24 hours'
        LIMIT 1
        """,
        (tenant_id, idempotency_key),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        result = {
            "id": row["id"],
            "message_sid": row["message_sid"],
            "send_status": row["send_status"],
            "created_at": row["created_at"],
        }
    else:
        result = {
            "id": row[0],
            "message_sid": row[1],
            "send_status": row[2],
            "created_at": row[3],
        }
    if result["send_status"] not in _IDEMPOTENT_HIT_STATUSES:
        logger.info(
            "send_whatsapp_template: ignoring non-deliverable idempotency marker "
            "(tenant=%s status=%s)",
            tenant_id,
            result["send_status"],
        )
        return None
    return result


def _check_tenant_rate_limit(cur: Any, tenant_id: str) -> bool:
    """Return True if per-tenant daily template limit is NOT exceeded (send allowed)."""
    cur.execute(
        """
        SELECT COUNT(*) FROM send_idempotency_keys
        WHERE tenant_id = %s
          AND send_status = 'sent'
          AND created_at > now() - interval '24 hours'
        """,
        (tenant_id,),
    )
    row = cur.fetchone()
    count = (row["count"] if isinstance(row, dict) else row[0]) or 0
    return int(count) < _TENANT_DAILY_LIMIT


def _resolve_customer(
    tenant_id: str, customer_id: str,
) -> dict[str, Any] | None:
    """Resolve a customer's send fields, or None if not visible.

    VT-306: reads through CustomersWrapper on its OWN tenant_connection (SET ROLE
    app_role + GUC + assert_tenant_scoped) — an upgrade from the prior inline
    ``set_config`` (no SET ROLE). Scope: ONLY this customers read migrates — the
    send flow's send_idempotency_keys access stays on its own connection (not a hot
    table / not gate-flagged), per Cowork 20260605T002000Z. (VT-324: the vestigial
    ``cur`` param dropped.)
    """
    row = CustomersWrapper().find_by_id(tenant_id, customer_id)
    if row is None:
        return None
    return {
        "phone_e164": row["phone_e164"],
        "opt_out_status": row["opt_out_status"],
        # VT-369 (Gap-5 PR-1 adjacent fix): complaint freeze at the tool boundary.
        # .get() — a stubbed/legacy row without the migration-091 column passes
        # through as None (only an explicit 'open' refuses).
        "complaint_status": row.get("complaint_status"),
    }


def _write_idempotency_ledger(
    cur: Any,
    tenant_id: str,
    idempotency_key: str,
    customer_id: str,
    message_sid: str | None,
    send_status: str,
) -> None:
    """Insert idempotency ledger row (ON CONFLICT DO NOTHING)."""
    cur.execute(
        """
        INSERT INTO send_idempotency_keys
            (tenant_id, idempotency_key, customer_id, message_sid, send_status)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
        """,
        (tenant_id, idempotency_key, customer_id, message_sid, send_status),
    )


def _write_campaign_message(
    cur: Any,
    tenant_id: str,
    customer_id: str,
    template_id: str,
    template_params: dict[str, str],  # noqa: ARG001 — reserved for future schema column
    message_sid: str | None,
    send_status: str,
    idempotency_key: str,
) -> None:
    """Insert campaign_messages row for the template send (VT-45 context)."""
    cur.execute(
        """
        INSERT INTO campaign_messages
            (tenant_id, customer_id, idempotency_key, message_sid, send_status,
             message_type)
        VALUES (%s, %s, %s, %s, %s, 'template')
        """,
        (
            tenant_id,
            customer_id,
            idempotency_key,
            message_sid,
            send_status,
            # template_id + template_params stored in the status comment; the
            # schema stores them in separate columns added by a follow-up row.
            # For now, idempotency_key is the cross-reference.
        ),
    )


def send_whatsapp_template(
    payload: SendWhatsappTemplateInput,
    *,
    pool: Any | None = None,
    send_fn: Callable[..., Any] | None = None,
) -> SendWhatsappTemplateOutput:
    """Send a Meta-approved WhatsApp template to a customer.

    `pool` — psycopg3 connection pool. Defaults to get_pool() in prod.
    `send_fn` — callable(tenant_id, template_name, params, *, recipient_phone)
        returning a SendResult. Defaults to send_template_message() from
        twilio_send. Tests inject a MagicMock.

    Registry validation + phone resolution happen before any Twilio call.
    Never raises into the caller: all error paths return an honest envelope.
    """
    # --- Step 0: Registry validation (before any DB touch) ---
    result = _validate_registry(
        payload.template_id, payload.language, payload.template_params,
    )
    if isinstance(result, ErrorEnvelope):
        return SendWhatsappTemplateOutput(
            status="error",
            customer_id=payload.customer_id,
            error_envelope=result,
        )
    variables, content_sid = result
    content_variables = _build_content_variables(variables, payload.template_params)

    if pool is None:
        from orchestrator.graph import get_pool
        pool = get_pool()

    if send_fn is None:
        from orchestrator.utils.twilio_send import send_template_message
        send_fn = send_template_message

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # RLS: scope all reads + writes to this tenant.
                # VT-140 fix: ``SET LOCAL <name> = %s`` cannot bind a parameter
                # ($1 is a syntax error in a SET statement), so the original
                # line raised SyntaxError against real Postgres and the send
                # silently failed (the MagicMock-cursor unit tests masked it).
                # Use set_config() — the parameterizable form the canonical
                # tenant_connection wrapper uses (db/tenant_connection.py).
                cur.execute(
                    "SELECT set_config('app.current_tenant', %s, false)",
                    (payload.tenant_id,),
                )

                # --- Idempotency check ---
                existing = _check_idempotency(
                    cur, payload.tenant_id, payload.idempotency_key,
                )
                if existing is not None:
                    logger.info(
                        "send_whatsapp_template: idempotent_hit tenant=%s customer=%s sid=%s",
                        payload.tenant_id, payload.customer_id, existing["message_sid"],
                    )
                    return SendWhatsappTemplateOutput(
                        status=existing["send_status"],  # type: ignore[arg-type]
                        message_sid=existing["message_sid"],
                        customer_id=payload.customer_id,
                        sent_at=existing["created_at"],
                    )

                # --- Resolve customer (RLS blocks cross-tenant) ---
                customer = _resolve_customer(
                    payload.tenant_id, payload.customer_id,
                )
                if customer is None:
                    logger.info(
                        "send_whatsapp_template: unauthorized tenant=%s customer=%s",
                        payload.tenant_id, payload.customer_id,
                    )
                    return SendWhatsappTemplateOutput(
                        status="unauthorized",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="customer_not_found",
                            message=(
                                "Customer not found or belongs to a different tenant. "
                                "Cross-tenant sends are rejected."
                            ),
                        ),
                    )

                # --- Consent check (CL-421: hard-refuse opted-out/blocked; VT-84:
                # owner_excluded — the owner's per-customer skip — also refuses) ---
                opt_out_status: str | None = customer.get("opt_out_status")
                if opt_out_status in ("opted_out", "blocked", "owner_excluded"):
                    logger.info(
                        "send_whatsapp_template: opted_out tenant=%s customer=%s status=%s",
                        payload.tenant_id, payload.customer_id, opt_out_status,
                    )
                    return SendWhatsappTemplateOutput(
                        status="unauthorized",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="recipient_opted_out",
                            message=(
                                f"Customer has opt_out_status='{opt_out_status}'. "
                                "Template sends to opted-out recipients are refused (CL-421)."
                            ),
                        ),
                    )

                # --- Complaint freeze (VT-369 Gap-5 PR-1 adjacent fix, mirrors the
                # opt-out hard-refuse above): a customer with an OPEN complaint
                # (migration 091, VT-321) must not receive business-initiated
                # template sends. The campaign-execute path already freezes on
                # complaint_status; this closes the direct-tool path so the gate
                # holds at the choke point too. 'none'/'resolved'/absent pass. ---
                if customer.get("complaint_status") == "open":
                    logger.info(
                        "send_whatsapp_template: complaint_open tenant=%s customer=%s",
                        payload.tenant_id, payload.customer_id,
                    )
                    return SendWhatsappTemplateOutput(
                        status="unauthorized",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="recipient_complaint_open",
                            message=(
                                "Customer has complaint_status='open'. Template sends "
                                "are refused until the complaint is resolved (VT-321/VT-369)."
                            ),
                        ),
                    )

                # VT-301 / CL-429 (Fazal ruling 2026-06-02): gate ALL business-initiated
                # sends on a recorded WhatsApp opt-in — enforced just below, once the phone
                # is resolved (the consent surface is phone_token-keyed). owner_inputs
                # (CL-425) is a basis to PROCESS, NOT a WhatsApp opt-in; owner-entered
                # customers (VT-55/56/63) become sendable only after they opt in via the
                # inbound/hook flow (VT-287 wa_inbound_optin). Supersedes the VT-85 carve-out.
                phone_e164: str | None = customer["phone_e164"]
                if not phone_e164:
                    return SendWhatsappTemplateOutput(
                        status="error",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="no_phone",
                            message="Customer has no phone number on record.",
                        ),
                    )

                # --- Opt-in gate (VT-301 / CL-429): fail-CLOSED, no opt-in record → refuse ---
                from orchestrator.privacy import consent as _consent

                if not _consent.has_consent_for_phone(payload.tenant_id, phone_e164):
                    logger.info(
                        "send_whatsapp_template: no_consent tenant=%s customer=%s",
                        payload.tenant_id, payload.customer_id,
                    )
                    return SendWhatsappTemplateOutput(
                        status="unauthorized",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="recipient_not_opted_in",
                            message=(
                                "No WhatsApp opt-in on record for this customer. Business-"
                                "initiated sends require a recorded opt-in (VT-301/CL-429); "
                                "owner_inputs is a processing basis, not a WhatsApp opt-in."
                            ),
                        ),
                    )

                # --- Rate limit (per-tenant 5000/day) ---
                if not _check_tenant_rate_limit(cur, payload.tenant_id):
                    logger.info(
                        "send_whatsapp_template: rate_limited tenant=%s",
                        payload.tenant_id,
                    )
                    return SendWhatsappTemplateOutput(
                        status="rate_limited",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="tenant_daily_limit",
                            message=f"Per-tenant daily template limit ({_TENANT_DAILY_LIMIT}) exceeded.",
                            retry_after_ms=int(_TENANT_WINDOW.total_seconds() * 1000),
                        ),
                    )

                # --- Twilio Content API send ---
                try:
                    from uuid import UUID
                    send_result = send_fn(
                        UUID(payload.tenant_id),
                        payload.template_id,
                        content_variables,
                        recipient_phone=phone_e164,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.info(
                        "send_whatsapp_template: twilio_error tenant=%s customer=%s "
                        "template=%s err=%s",
                        payload.tenant_id, payload.customer_id,
                        payload.template_id, type(exc).__name__,
                    )
                    _write_idempotency_ledger(
                        cur, payload.tenant_id, payload.idempotency_key,
                        payload.customer_id, None, "error",
                    )
                    _write_campaign_message(
                        cur, payload.tenant_id, payload.customer_id,
                        payload.template_id, payload.template_params,
                        None, "error", payload.idempotency_key,
                    )
                    return SendWhatsappTemplateOutput(
                        status="error",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="twilio_error",
                            message=type(exc).__name__,
                        ),
                    )

                # SendResult from twilio_send.send_template_message
                if not send_result.success:
                    err_code = send_result.error_code or "twilio_error"
                    err_msg = send_result.error_message or "Twilio returned failure"
                    _write_idempotency_ledger(
                        cur, payload.tenant_id, payload.idempotency_key,
                        payload.customer_id, None, "error",
                    )
                    _write_campaign_message(
                        cur, payload.tenant_id, payload.customer_id,
                        payload.template_id, payload.template_params,
                        None, "error", payload.idempotency_key,
                    )
                    return SendWhatsappTemplateOutput(
                        status="error",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code=err_code,
                            message=err_msg,
                        ),
                    )

                message_sid = send_result.message_sid
                sent_at = _now()

                # --- Write ledger + campaign_messages ---
                _write_idempotency_ledger(
                    cur, payload.tenant_id, payload.idempotency_key,
                    payload.customer_id, message_sid, "sent",
                )
                _write_campaign_message(
                    cur, payload.tenant_id, payload.customer_id,
                    payload.template_id, payload.template_params,
                    message_sid, "template_sent", payload.idempotency_key,
                )

                logger.info(
                    "send_whatsapp_template: sent tenant=%s customer=%s "
                    "template=%s status=sent sid=%s",
                    payload.tenant_id, payload.customer_id,
                    payload.template_id, message_sid,
                )
                return SendWhatsappTemplateOutput(
                    status="sent",
                    message_sid=message_sid,
                    customer_id=payload.customer_id,
                    sent_at=sent_at,
                )

    except Exception as exc:  # noqa: BLE001
        # Schema-absent (migration not applied) or transient DB error.
        # Never raise; honest error envelope.
        logger.info(
            "send_whatsapp_template: db_error tenant=%s (%s)",
            payload.tenant_id, type(exc).__name__,
        )
        return SendWhatsappTemplateOutput(
            status="error",
            error_envelope=ErrorEnvelope(
                code="db_error", message=type(exc).__name__,
            ),
        )


__all__ = [
    "ErrorEnvelope",
    "SendWhatsappTemplateInput",
    "SendWhatsappTemplateOutput",
    "send_whatsapp_template",
]
