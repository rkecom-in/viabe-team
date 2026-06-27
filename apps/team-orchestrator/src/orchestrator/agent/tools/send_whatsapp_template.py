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

# VT-420 — the pre-send in-flight marker. Written + committed BEFORE the Twilio
# messages.create call and flipped to 'sent' after success. A 'sending' row that
# is STILL 'sending' on a later attempt means the process crashed in the window
# between Twilio dispatch and the 'sent' commit: the message was PROBABLY already
# sent, so the recovery attempt must NOT re-send (a re-send would double-charge /
# double-message). It is a HIT (block the re-send) but is handled distinctly from
# the deliverable statuses above because its message_sid is unknown (NULL) and it
# wants its own fail-SAFE log line (flag for review). NOT in _IDEMPOTENT_HIT_STATUSES
# so the plain echo path doesn't fire — _check_idempotency surfaces it explicitly.
_INFLIGHT_STATUS = "sending"

# VT-423 — a 'sending' marker older than this is "stuck" (a normal send resolves to a
# terminal status in seconds). It STILL blocks the re-send (never auto-re-send), but
# emits a loud log so a reconciler / Ops can resolve the genuinely-orphaned marker to a
# terminal flagged state. Threshold is generous — well past any plausible Twilio-call +
# commit latency — so a legitimately in-flight send is never mislabelled stuck.
_STALE_MARKER_AGE = timedelta(hours=24)


def _warn_if_stale_marker(
    tenant_id: str, idempotency_key: str, created_at: Any,
) -> None:
    """VT-423: loudly flag a 'sending' marker that is older than _STALE_MARKER_AGE.

    The marker still blocks the re-send (the caller treats it as an in-flight hit) — we
    NEVER auto-re-send a stale marker, because we cannot prove the original message was
    NOT delivered. This log is the hand-off to a reconciler / Ops to resolve the stuck
    row to a terminal flagged-for-review state. Best-effort: a non-datetime created_at
    (e.g. a stub) is silently ignored — it must never break the send path."""
    try:
        age = _now() - created_at
    except TypeError:
        return
    if age > _STALE_MARKER_AGE:
        logger.warning(
            "send_whatsapp_template: stale_inflight_marker tenant=%s key=%s age_hours=%.1f "
            "— a 'sending' marker has been unresolved past %dh; STILL blocking the re-send "
            "(NOT auto-re-sending — message may have been delivered). Flag for review: a "
            "reconciler should resolve this stuck marker to a terminal state.",
            tenant_id, idempotency_key, age.total_seconds() / 3600.0,
            int(_STALE_MARKER_AGE.total_seconds() // 3600),
        )


def _check_idempotency(
    cur: Any, tenant_id: str, idempotency_key: str,
) -> dict[str, Any] | None:
    """Return existing ledger row if the key is still an idempotent hit, else None.

    Returns None for a row whose send_status the output cannot represent (VT-262)
    — the caller re-evaluates (consent gate / send) rather than echoing an
    invalid status.

    VT-420: a 'sending' (in-flight) row IS returned (it is a recovery hit that must
    block the re-send) even though 'sending' is not in _IDEMPOTENT_HIT_STATUSES —
    the caller detects it via send_status == _INFLIGHT_STATUS and reports a fail-SAFE
    'probably already sent' terminal outcome rather than re-dispatching.

    VT-423 (stale-marker window, residual #2): a 'sending' marker must block the
    re-send REGARDLESS OF AGE. The original query bounded EVERY row to a 24h
    created_at window, so a draft re-driven >24h after a crash found the stale
    'sending' marker had fallen out of the window → SELECT returned None → re-send
    (money-UNSAFE: a possible-already-delivered message re-fires after a day). Fix:
    the time bound now applies ONLY to the TERMINAL deliverable statuses (whose 24h
    idempotency TTL is the legitimate dedup window). The fail-SAFE 'sending' marker
    is NOT time-bounded — it blocks re-dispatch until it resolves to a terminal state
    ('sent'/'error') or a separate reconciler sweeps a genuinely-stuck marker to a
    terminal flagged state. The tool NEVER auto-re-sends a stale 'sending' row; an
    old one gets a loud log so the reconciler/Ops can resolve it. This GUARANTEES no
    double-send across the window: a 'sending' marker only ever stops blocking when
    something DELIBERATELY resolves it, never by silent expiry.
    """
    cur.execute(
        """
        SELECT id::text, message_sid, send_status, created_at
        FROM send_idempotency_keys
        WHERE tenant_id = %s
          AND idempotency_key = %s
          AND (
                send_status = 'sending'                       -- in-flight: never expires
                OR created_at > now() - interval '24 hours'   -- terminal: 24h idem TTL
          )
        ORDER BY (send_status = 'sending') DESC, created_at DESC
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
    # VT-420/VT-423: a 'sending' in-flight marker is a recovery HIT — surface it so the
    # caller blocks the re-send (the message was probably already dispatched), at ANY
    # age. It is NOT in _IDEMPOTENT_HIT_STATUSES, so allow it through explicitly here.
    if result["send_status"] == _INFLIGHT_STATUS:
        _warn_if_stale_marker(tenant_id, idempotency_key, result["created_at"])
        return result
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


def _write_inflight_marker(
    cur: Any,
    tenant_id: str,
    idempotency_key: str,
    customer_id: str,
) -> bool:
    """VT-420: write the pre-send 'sending' (in-flight) marker, committed BEFORE the
    Twilio messages.create call (the pool is autocommit, so this standalone INSERT
    commits the instant it executes — that durable commit is the whole point).

    VT-423 (self-serializing — residual #1): the statement is a CONDITIONAL upsert that
    CLAIMS the (tenant_id, idempotency_key) row for THIS attempt and reports, via RETURNING
    id / rowcount, whether the claim succeeded:

        INSERT ... 'sending'
        ON CONFLICT (tenant_id, idempotency_key) DO UPDATE
            SET send_status='sending', message_sid=NULL, customer_id=EXCLUDED.customer_id
            WHERE send_idempotency_keys.send_status NOT IN ('sent','sending')
        RETURNING id

      • No row exists → the INSERT lands → RETURNING id → rowcount=1 → CLAIMED. This attempt
        OWNS the send and proceeds to Twilio.
      • An existing 'sending' row (a TRUE-parallel sibling that won the INSERT race, OR a
        crash-orphaned marker) → ON CONFLICT, but the WHERE excludes 'sending' → DO UPDATE
        matches 0 rows → NO RETURNING row → rowcount=0 → NOT claimed → this attempt LOST
        and must NOT send (the in-flight sibling owns it).
      • An existing 'sent' row → WHERE excludes 'sent' → 0 rows → rowcount=0 → NOT claimed →
        must NOT send (defense in depth; _check_idempotency already caught this as a hit).
      • An existing RETRYABLE row ('error'/'rate_limited'/'window_closed' — which
        _check_idempotency deliberately returns None for, clearing the retry, VT-387) →
        the WHERE matches → DO UPDATE flips it to a fresh 'sending' → RETURNING id →
        rowcount=1 → CLAIMED → proceed. The marker takes the row over for the legit retry
        WITHOUT a second row, and the next reject/crash window is correctly re-armed.

    THIS is what makes the TOOL self-serialize two TRUE-parallel first-attempts on one key:
    Postgres' UNIQUE(tenant_id, idempotency_key) lets exactly ONE concurrent statement win
    the INSERT; the loser blocks on the row lock, then the conditional DO UPDATE finds the
    winner's row already 'sending' → its WHERE excludes 'sending' → 0 rows updated → 0
    claimed. So at most ONE attempt ever reaches messages.create — the fix no longer DEPENDS
    on upstream DBOS workflow-id single-flight or the serial draft loop (VT-420 residual #1).
    The _check_idempotency gate ahead of this still catches the common in-flight / idempotent
    hits early; this claim check closes the narrow true-parallel window where two callers both
    saw None before either wrote a marker.

    Returns True iff this attempt CLAIMED the row (may send), False iff it lost (must not)."""
    cur.execute(
        """
        INSERT INTO send_idempotency_keys
            (tenant_id, idempotency_key, customer_id, message_sid, send_status)
        VALUES (%s, %s, %s, NULL, 'sending')
        ON CONFLICT (tenant_id, idempotency_key) DO UPDATE
            SET send_status = 'sending',
                message_sid = NULL,
                customer_id = EXCLUDED.customer_id
            WHERE send_idempotency_keys.send_status NOT IN ('sent', 'sending')
        RETURNING id
        """,
        (tenant_id, idempotency_key, customer_id),
    )
    # psycopg3 sets rowcount to 1 when this attempt INSERTed-or-claimed the row and 0 when
    # the conditional DO UPDATE matched nothing (a sibling already holds 'sending', or the
    # row is terminal 'sent') — the authoritative claim signal (the `cur.rowcount` check the
    # VT-423 contract calls for). Prefer it; fall back to RETURNING's fetchone() only when a
    # cursor doesn't surface a usable rowcount (>= 0 means it's meaningful here).
    rowcount = getattr(cur, "rowcount", None)
    if isinstance(rowcount, int) and rowcount >= 0:
        return rowcount == 1
    return cur.fetchone() is not None


def _write_idempotency_ledger(
    cur: Any,
    tenant_id: str,
    idempotency_key: str,
    customer_id: str,
    message_sid: str | None,
    send_status: str,
) -> None:
    """Upsert the idempotency ledger row to its terminal status.

    VT-420: a 'sending' in-flight marker (written by _write_inflight_marker BEFORE the
    Twilio call) already occupies this (tenant_id, idempotency_key), so a plain INSERT
    ... ON CONFLICT DO NOTHING would be a no-op and leave the row stuck at 'sending'.
    Upsert instead — flip the existing marker to its terminal status ('sent' on a
    delivered send, 'error' on a Twilio reject/raise) and stamp the message_sid. The
    'sent' flip is the OTHER side of the crash window: once it commits, recovery sees
    'sent' (an idempotent hit) and never re-sends; if the process dies before it
    commits, the row stays 'sending' and recovery blocks the re-send fail-SAFE.

    NEVER downgrade a terminal 'sent' back to 'error': the WHERE clause guards the flip
    so a late/duplicate error-path write cannot clobber a delivered send (defense in
    depth — the linear flow never does this, but the guard makes it structurally safe)."""
    cur.execute(
        """
        INSERT INTO send_idempotency_keys
            (tenant_id, idempotency_key, customer_id, message_sid, send_status)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, idempotency_key) DO UPDATE
            SET send_status = EXCLUDED.send_status,
                message_sid = EXCLUDED.message_sid
            WHERE send_idempotency_keys.send_status <> 'sent'
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
        from functools import partial

        from orchestrator.utils.twilio_send import send_template_message

        # VT-460 gap (c): the VT-45 tool is the SINGLE gated chokepoint every customer template send
        # (agent + campaign) funnels through. Mark the real transport call is_customer_send=True so
        # the transport's structural choke admits it ONLY inside customer_send_context() (the gated
        # callers enter that context). An injected test send_fn never reaches the real transport, so
        # the flag is bound only on this default. The choke also fails closed if this tool were ever
        # called outside the context.
        send_fn = partial(send_template_message, is_customer_send=True)

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
                    # VT-420 crash recovery: a STILL-'sending' marker means a prior
                    # attempt dispatched to Twilio but died before the 'sent' commit —
                    # the message was PROBABLY already delivered. Fail SAFE: do NOT
                    # re-send (a re-send double-charges). Report a terminal 'sent'
                    # outcome with NO message_sid (we never recorded it — honest) so the
                    # caller marks the draft terminal and stops retrying. A loud
                    # flag-for-review line distinguishes it from a clean send.
                    if existing["send_status"] == _INFLIGHT_STATUS:
                        logger.warning(
                            "send_whatsapp_template: inflight_recovery tenant=%s customer=%s "
                            "key=%s — a 'sending' marker survived a crash; NOT re-sending "
                            "(message probably already delivered). Flag for review: SID unknown.",
                            payload.tenant_id, payload.customer_id, payload.idempotency_key,
                        )
                        return SendWhatsappTemplateOutput(
                            status="sent",
                            message_sid=None,
                            customer_id=payload.customer_id,
                            sent_at=existing["created_at"],
                        )
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

                # --- VT-420: pre-send in-flight marker (the crash-window close) ---
                # Write + commit the 'sending' marker BEFORE the irreversible Twilio
                # call (the autocommit pool commits this standalone INSERT instantly).
                # Twilio's Messages/Content API has NO native idempotency key (twilio
                # 9.10.9 messages.create exposes none; the official Messages REST docs
                # document none — I-Twilio-Idempotency-Token is a webhook header, the
                # reverse direction), so this marker is the money-SAFE alternative: if
                # the process crashes AFTER Twilio dispatch but BEFORE the 'sent' flip,
                # the durable 'sending' row makes recovery block the re-send (no
                # double-charge) instead of re-firing on a missing key.
                won_marker = _write_inflight_marker(
                    cur, payload.tenant_id, payload.idempotency_key,
                    payload.customer_id,
                )
                # VT-423 self-serialize: if the marker INSERT lost the ON-CONFLICT race
                # (won_marker False), a TRUE-parallel first-attempt on this same key
                # already owns the row and is mid-flight to Twilio. Both attempts passed
                # _check_idempotency seeing None (the race window), but only the INSERT
                # winner may send. The loser fails SAFE here: do NOT call Twilio (that
                # would be the double-send), report the same probably-already-delivered
                # terminal 'sent' (no SID) the crash-recovery path returns.
                if not won_marker:
                    logger.warning(
                        "send_whatsapp_template: inflight_race_lost tenant=%s customer=%s "
                        "key=%s — a concurrent attempt already holds the 'sending' marker; "
                        "NOT sending (the marker self-serializes the double-send). SID unknown.",
                        payload.tenant_id, payload.customer_id, payload.idempotency_key,
                    )
                    return SendWhatsappTemplateOutput(
                        status="sent",
                        message_sid=None,
                        customer_id=payload.customer_id,
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
