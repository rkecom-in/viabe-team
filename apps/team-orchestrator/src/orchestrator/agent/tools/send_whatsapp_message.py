"""VT-44 — send_whatsapp_message standalone tool.

Delivers a free-form WhatsApp message via Twilio. STRICT 24-hour window
enforcement: if customers.last_inbound_at is NULL or older than 24 hours,
returns window_closed and instructs the caller to use send_whatsapp_template
(VT-45). The tool never silently substitutes a template send (Pillar 7).

Pillars
- Pillar 1: pure transport. No LLM, no content inspection.
- Pillar 2: deterministic execution. Idempotent on (tenant_id, idempotency_key).
- Pillar 3: phone resolved internally; phone_e164 never in input or logs (CL-390).
- Pillar 7: honest window enforcement; window_closed → caller decides, not tool.

Rate limits (Decision D3: derived from COUNT over send_idempotency_keys):
- Per-tenant: 1000 freeform sends per 24h.
- Per-customer: 1 freeform send per 6h.

NO PII (CL-390): logged fields = customer_id, tenant_id, message_sid, status.
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

# Rate-limit constants (Decision D4, plan-confirmed).
_TENANT_DAILY_LIMIT = 1000       # per-tenant freeform sends per 24h
_CUSTOMER_6H_LIMIT = 1           # per-customer freeform sends per 6h
_CUSTOMER_WINDOW = timedelta(hours=6)
_TENANT_WINDOW = timedelta(hours=24)
_INBOUND_WINDOW = timedelta(hours=24)


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    retry_after_ms: int | None = None


class SendWhatsAppMessageInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., min_length=1)
    customer_id: str = Field(..., min_length=1)   # UUID as str; resolved to phone internally
    body: str = Field(..., min_length=1, max_length=4096)
    idempotency_key: str = Field(..., min_length=1)


class SendWhatsAppMessageOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["sent", "window_closed", "rate_limited", "unauthorized", "error"]
    message_sid: str | None = None
    customer_id: str | None = None
    sent_at: datetime | None = None
    error_envelope: ErrorEnvelope | None = None


def _now() -> datetime:
    return datetime.now(UTC)


# VT-262: statuses the output Literal can represent. A ledger row whose
# send_status is NOT one of these (e.g. a 'skipped' consent marker written by the
# campaign-execute seam, which shares send_idempotency_keys) is NOT a prior
# deliverable send — echoing it would raise a pydantic ValidationError that the
# broad except turns into a phantom db_error AND wrongly suppress the send.
#
# VT-410 (sibling of VT-387, which fixed the same bug in send_whatsapp_template):
# 'error' is DELIBERATELY excluded (it WAS in the VT-262 set). A freeform send that
# TRANSIENTLY failed (Twilio 5xx, network blip, 4xx reject) caches send_status='error'
# under the caller-supplied idempotency_key — treating that as an idempotent hit made
# the freeform send unretryable for the key's full 24h TTL, so a retry within the
# window silently no-opped. Excluding 'error' makes _check_idempotency return None for
# an errored row → the caller re-runs every gate (opt-out/window/caps/rate) and re-sends.
#
# Double-send safety (the load-bearing invariant) — note how the FREEFORM path differs
# from the template path: send_freeform_message() (twilio_send) does NOT catch
# TwilioRestException — it lets ALL provider errors (4xx, 5xx, network, ValueError)
# PROPAGATE and returns a SID ONLY on a delivered message. So unlike the template tool
# (which has TWO 'error' producers: send_fn raising AND a 4xx success=False result),
# the freeform tool writes 'error' to the ledger in exactly ONE place: the `except
# Exception` around `send_fn(...)`. That except fires ONLY when messages.create() raised
# — the provider did NOT accept the message, no side-effect occurred. The two other
# 'error' RETURNS (the no_phone guard and the outer db_error except) write NOTHING to the
# ledger. 'sent' is written ONLY after send_fn returns a real SID (a delivered message),
# and 'sent' STAYS in the set — so a completed/sent freeform message remains an idempotent
# hit and never re-sends. 'error' is therefore never written post-side-effect → dropping
# it cannot cause a double-send.
_IDEMPOTENT_HIT_STATUSES = frozenset(
    {"sent", "window_closed", "rate_limited", "unauthorized"}
)


def _check_idempotency(
    cur: Any, tenant_id: str, idempotency_key: str
) -> dict[str, Any] | None:
    """Return the existing ledger row if the key was already used, else None.

    TTL = 24h: keys older than 24h are treated as expired (may re-send). Returns
    None for a row whose send_status the output cannot represent (VT-262) — the
    caller re-evaluates rather than echoing an invalid status.
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
        result = {"id": row["id"], "message_sid": row["message_sid"],
                  "send_status": row["send_status"], "created_at": row["created_at"]}
    else:
        result = {"id": row[0], "message_sid": row[1],
                  "send_status": row[2], "created_at": row[3]}
    if result["send_status"] not in _IDEMPOTENT_HIT_STATUSES:
        logger.info(
            "send_whatsapp_message: ignoring non-deliverable idempotency marker "
            "(tenant=%s status=%s)",
            tenant_id, result["send_status"],
        )
        return None
    return result


def _check_tenant_rate_limit(cur: Any, tenant_id: str) -> bool:
    """Return True if the per-tenant daily limit is NOT exceeded (send allowed)."""
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


def _check_customer_rate_limit(cur: Any, tenant_id: str, customer_id: str) -> bool:
    """Return True if the per-customer 6h limit is NOT exceeded (send allowed)."""
    cur.execute(
        """
        SELECT COUNT(*) FROM send_idempotency_keys
        WHERE tenant_id = %s
          AND customer_id = %s
          AND send_status = 'sent'
          AND created_at > now() - interval '6 hours'
        """,
        (tenant_id, customer_id),
    )
    row = cur.fetchone()
    count = (row["count"] if isinstance(row, dict) else row[0]) or 0
    return int(count) < _CUSTOMER_6H_LIMIT


def _resolve_customer(
    tenant_id: str, customer_id: str
) -> dict[str, Any] | None:
    """Resolve a customer's send fields, or None if not visible (cross-tenant or
    absent).

    VT-306: reads through CustomersWrapper on its OWN tenant_connection (SET ROLE
    app_role + GUC + assert_tenant_scoped) — an upgrade from the prior inline
    ``set_config`` (no SET ROLE). Scope: ONLY this customers read migrates — the
    send flow's send_idempotency_keys access stays on its own connection (not a hot
    table), per Cowork 20260605T002000Z. (VT-324: vestigial ``cur`` param dropped.)
    """
    row = CustomersWrapper().find_by_id(tenant_id, customer_id)
    if row is None:
        return None
    return {
        "phone_e164": row["phone_e164"],
        "last_inbound_at": row["last_inbound_at"],
        # VT-369 (Gap-5 PR-1 fix): the freeform path was missing the opt-out
        # gate. .get() — a stubbed row without the column passes as None.
        "opt_out_status": row.get("opt_out_status"),
    }


def _write_ledger(
    cur: Any,
    tenant_id: str,
    idempotency_key: str,
    customer_id: str,
    message_sid: str | None,
    send_status: str,
) -> None:
    """Insert idempotency ledger row (ON CONFLICT DO NOTHING — safe to call twice)."""
    cur.execute(
        """
        INSERT INTO send_idempotency_keys
            (tenant_id, idempotency_key, customer_id, message_sid, send_status)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
        """,
        (tenant_id, idempotency_key, customer_id, message_sid, send_status),
    )


def send_whatsapp_message(
    payload: SendWhatsAppMessageInput,
    *,
    pool: Any | None = None,
    send_fn: Callable[[str, str], str] | None = None,
) -> SendWhatsAppMessageOutput:
    """Send a free-form WhatsApp message to a customer within their 24h window.

    `pool` — psycopg3 connection pool. Defaults to get_pool() in prod.
    `send_fn` — callable(body, recipient_phone) -> message_sid. Defaults to
        send_freeform_message() from twilio_send. Tests inject a MagicMock.

    Never raises into the caller. All error paths return an error envelope.
    """
    if pool is None:
        from orchestrator.graph import get_pool
        pool = get_pool()

    if send_fn is None:
        from orchestrator.utils.twilio_send import send_freeform_message
        send_fn = send_freeform_message

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # RLS: scope all reads + writes to this tenant.
                cur.execute(
                    "SELECT set_config('app.current_tenant', %s, false)",
                    (payload.tenant_id,),
                )

                # --- Idempotency check ---
                existing = _check_idempotency(cur, payload.tenant_id, payload.idempotency_key)
                if existing is not None:
                    logger.info(
                        "send_whatsapp_message: idempotent_hit tenant=%s customer=%s sid=%s",
                        payload.tenant_id, payload.customer_id, existing["message_sid"],
                    )
                    return SendWhatsAppMessageOutput(
                        status=existing["send_status"],  # type: ignore[arg-type]
                        message_sid=existing["message_sid"],
                        customer_id=payload.customer_id,
                        sent_at=existing["created_at"],
                    )

                # --- Resolve customer (RLS blocks cross-tenant) ---
                customer = _resolve_customer(payload.tenant_id, payload.customer_id)
                if customer is None:
                    logger.info(
                        "send_whatsapp_message: unauthorized tenant=%s customer=%s",
                        payload.tenant_id, payload.customer_id,
                    )
                    return SendWhatsAppMessageOutput(
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

                # --- Opt-out gate (VT-369 Gap-5 PR-1 fix — was MISSING here; CL-421
                # dual-layer refuse, mirroring send_whatsapp_template): an opted-out
                # customer who messages in re-opens a 24h *window*, not consent. An
                # in-window freeform send to an opted_out/blocked/owner_excluded
                # recipient is refused BEFORE any window/rate evaluation. ---
                if customer.get("opt_out_status") in (
                    "opted_out", "blocked", "owner_excluded",
                ):
                    logger.info(
                        "send_whatsapp_message: opted_out tenant=%s customer=%s status=%s",
                        payload.tenant_id, payload.customer_id,
                        customer.get("opt_out_status"),
                    )
                    return SendWhatsAppMessageOutput(
                        status="unauthorized",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="recipient_opted_out",
                            message=(
                                "Customer has opt_out_status="
                                f"'{customer.get('opt_out_status')}'. Freeform sends to "
                                "opted-out recipients are refused even in-window "
                                "(CL-421/VT-369)."
                            ),
                        ),
                    )

                phone_e164: str | None = customer["phone_e164"]
                last_inbound_at: datetime | None = customer["last_inbound_at"]

                # --- 24-hour window enforcement (Pillar 7) ---
                if last_inbound_at is None:
                    logger.info(
                        "send_whatsapp_message: window_closed no_inbound_history "
                        "tenant=%s customer=%s",
                        payload.tenant_id, payload.customer_id,
                    )
                    return SendWhatsAppMessageOutput(
                        status="window_closed",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="no_inbound_history",
                            message=(
                                "Customer has no inbound message history. "
                                "Use send_whatsapp_template (VT-45) to initiate contact."
                            ),
                        ),
                    )

                # Normalise to UTC-aware for comparison.
                if last_inbound_at.tzinfo is None:
                    last_inbound_at = last_inbound_at.replace(tzinfo=UTC)
                window_cutoff = _now() - _INBOUND_WINDOW
                if last_inbound_at < window_cutoff:
                    logger.info(
                        "send_whatsapp_message: window_closed window_expired "
                        "tenant=%s customer=%s last_inbound=%s",
                        payload.tenant_id, payload.customer_id,
                        last_inbound_at.isoformat(),
                    )
                    return SendWhatsAppMessageOutput(
                        status="window_closed",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="window_expired",
                            message=(
                                "Customer's 24-hour messaging window has expired. "
                                "Use send_whatsapp_template (VT-45) to re-engage."
                            ),
                        ),
                    )

                # --- Phone must be present ---
                if not phone_e164:
                    return SendWhatsAppMessageOutput(
                        status="error",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="no_phone",
                            message="Customer has no phone number on record.",
                        ),
                    )

                # --- Rate limits (Decision D3: COUNT over ledger) ---
                if not _check_tenant_rate_limit(cur, payload.tenant_id):
                    logger.info(
                        "send_whatsapp_message: rate_limited per_tenant tenant=%s",
                        payload.tenant_id,
                    )
                    return SendWhatsAppMessageOutput(
                        status="rate_limited",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="tenant_daily_limit",
                            message=f"Per-tenant daily limit ({_TENANT_DAILY_LIMIT}) exceeded.",
                            retry_after_ms=int(_TENANT_WINDOW.total_seconds() * 1000),
                        ),
                    )

                if not _check_customer_rate_limit(cur, payload.tenant_id, payload.customer_id):
                    logger.info(
                        "send_whatsapp_message: rate_limited per_customer tenant=%s customer=%s",
                        payload.tenant_id, payload.customer_id,
                    )
                    return SendWhatsAppMessageOutput(
                        status="rate_limited",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="customer_6h_limit",
                            message="Per-customer 6-hour send limit exceeded.",
                            retry_after_ms=int(_CUSTOMER_WINDOW.total_seconds() * 1000),
                        ),
                    )

                # --- Twilio send (via injected send_fn or real helper) ---
                try:
                    message_sid = send_fn(payload.body, phone_e164)
                except Exception as exc:  # noqa: BLE001
                    logger.info(
                        "send_whatsapp_message: twilio_error tenant=%s customer=%s err=%s",
                        payload.tenant_id, payload.customer_id, type(exc).__name__,
                    )
                    _write_ledger(
                        cur, payload.tenant_id, payload.idempotency_key,
                        payload.customer_id, None, "error",
                    )
                    return SendWhatsAppMessageOutput(
                        status="error",
                        customer_id=payload.customer_id,
                        error_envelope=ErrorEnvelope(
                            code="twilio_error",
                            message=type(exc).__name__,
                        ),
                    )

                sent_at = _now()
                # --- Write idempotency ledger row ---
                _write_ledger(
                    cur, payload.tenant_id, payload.idempotency_key,
                    payload.customer_id, message_sid, "sent",
                )

                logger.info(
                    "send_whatsapp_message: sent tenant=%s customer=%s sid=%s",
                    payload.tenant_id, payload.customer_id, message_sid,
                )
                return SendWhatsAppMessageOutput(
                    status="sent",
                    message_sid=message_sid,
                    customer_id=payload.customer_id,
                    sent_at=sent_at,
                )

    except Exception as exc:  # noqa: BLE001
        # Schema-absent (migration not applied) or transient DB error.
        # Never raise; honest error envelope.
        logger.info(
            "send_whatsapp_message: db_error tenant=%s (%s)",
            payload.tenant_id, type(exc).__name__,
        )
        return SendWhatsAppMessageOutput(
            status="error",
            error_envelope=ErrorEnvelope(
                code="db_error", message=type(exc).__name__
            ),
        )


__all__ = [
    "ErrorEnvelope",
    "SendWhatsAppMessageInput",
    "SendWhatsAppMessageOutput",
    "send_whatsapp_message",
]
