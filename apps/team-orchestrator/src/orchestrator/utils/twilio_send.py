"""Twilio template-send helper for the direct handlers (VT-3.3c).

Pillar 1: pure send mechanics — no reasoning, no LLM.
Pillar 3: the recipient phone is tokenised in every SendResult; never logged
          or returned in plaintext.
Pillar 7: SendResult honestly reflects the Twilio response — there is no
          hardcoded success. A failed send returns success=False.
Pillar 8: template *content* lives in the Twilio Console + the Meta WABA;
          config/twilio_templates.yaml is a name->content_sid mapping only.

Idempotency: send_template_message is a ``@DBOS.step`` — once it completes,
DBOS checkpoints the SendResult and never re-executes it on workflow replay.
Twilio's Messages API (twilio 9.x) has no idempotency-key parameter, so the
only residual duplicate-send window is a crash after the Twilio call but
before the DBOS checkpoint commits — accepted at Phase 1 scale.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

from dbos import DBOS
from pydantic import BaseModel
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from orchestrator.db import tenant_connection
from orchestrator.templates_registry import (
    UnknownTemplateError,
    resolve as _registry_resolve,
)
from orchestrator.utils.dev_send_guard import maybe_wrap_for_dev
from orchestrator.utils.phone_token import hash_phone

logger = logging.getLogger(__name__)

_TEMPLATES_FILE = Path(__file__).resolve().parents[3] / "config" / "twilio_templates.yaml"


# --- VT-460 gap (c): transport-level structural customer-send choke ----------------------------
#
# The rail-harness finding: `send_template_message`/`send_freeform_message` dispatch to ANY phone
# with valid creds. The brain is structurally barred from holding a send tool (VT-268), and the
# agent + campaign customer-send paths run the full deterministic gate stack — but the TRANSPORT
# itself had no structural boundary. A FUTURE direct caller passing a CUSTOMER phone would bypass
# every gate; only convention + the lint + review stood in the way (NOT a structural choke).
#
# This makes the transport itself FAIL CLOSED for un-gated customer sends. A send EXPLICITLY FLAGGED
# as customer-bound — a template send with `is_customer_send=True` (set ONLY by the VT-45 tool, the
# single chokepoint the agent + campaign paths funnel through) or a freeform with
# `is_customer_session=True` (the VT-287 inbound session class) — MUST be issued from inside
# `customer_send_context()`. The legitimate customer paths enter that context after their
# deterministic gate stack. A new direct caller that flags a customer send but forgets the context
# raises `UngatedCustomerSendError` rather than silently sending.
#
# WHY AN EXPLICIT FLAG, NOT THE REGISTRY `audience`: some `audience: customer` templates
# (team_opt_out_confirmation, team_status_ping) are sent BY owner-reply handlers TO the owner — the
# audience field labels the template's typical reader, NOT whether THIS dispatch targets an
# end-customer. Only the caller knows, so the caller flags it.
#
# OWNER sends are exempt and UNCHANGED: every owner template (default is_customer_send=False) + owner
# freeforms (ops_resolve, business_plan/delivery, breach_notification, onboarding,
# request_owner_approval, the owner-reply direct_handlers, l3_hold presend-notice) carry no flag and
# never enter the context.

_GATED_CUSTOMER_SEND: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "viabe_gated_customer_send", default=False
)


class UngatedCustomerSendError(RuntimeError):
    """Raised when a CUSTOMER-bound send is attempted OUTSIDE ``customer_send_context()``.

    The structural backstop (VT-460 gap c): a customer send that did not route through a gated
    choke (the VT-45 tool's deterministic gate stack, or the VT-287 inbound session class) fails
    CLOSED at the transport rather than reaching Twilio. Owner sends never trip this.
    """


@contextmanager
def customer_send_context() -> Iterator[None]:
    """Mark the dynamic extent of a GATED customer send.

    Entered ONLY by a caller that has already run (or is about to run, in the same call) the
    deterministic customer-send choke — `agents.customer_send_choke.assert_customer_send_allowed`
    (onboarded + WABA-live) plus the per-recipient consent/opt-out/caps stack. The transport
    permits a customer-bound dispatch only while this context is active. Re-entrant (nested
    gated sends are fine); the token restores the prior value on exit.
    """
    token = _GATED_CUSTOMER_SEND.set(True)
    try:
        yield
    finally:
        _GATED_CUSTOMER_SEND.reset(token)


def _assert_gated_if_customer(*, is_customer: bool, template_name: str, recipient_token: str) -> None:
    """Fail-CLOSED transport boundary: a customer-bound send MUST be inside ``customer_send_context``.

    ``is_customer`` is an EXPLICIT caller flag (``is_customer_send`` for templates, set only by the
    VT-45 tool; ``is_customer_session`` for freeforms, set only by the VT-287 inbound path) — not the
    registry audience (some audience:customer templates are owner-reply sends). Owner sends pass
    ``is_customer=False`` and are never checked. Raises ``UngatedCustomerSendError`` (before any
    Twilio call) when a flagged customer send is issued outside the gated context.
    """
    if is_customer and not _GATED_CUSTOMER_SEND.get():
        raise UngatedCustomerSendError(
            f"un-gated customer send refused at the transport: template={template_name!r} "
            f"-> {recipient_token}. Customer sends MUST route through customer_send_context() "
            "after the deterministic send choke (VT-460 gap c); a direct transport call to a "
            "customer is a structural boundary breach."
        )


class SendResult(BaseModel):
    """Outcome of one template send. Persisted by callers; PII-safe."""

    success: bool
    message_sid: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    attempted_at: datetime
    template_name: str
    recipient_phone_token: str  # hash_phone() token — never plaintext


# TemplateNotConfigured is an alias for UnknownTemplateError (D4, VT-163).
# Kept here for back-compat: existing callers that catch TemplateNotConfigured
# continue to work unchanged; the registry raises UnknownTemplateError which IS
# TemplateNotConfigured.
TemplateNotConfigured = UnknownTemplateError


def _templates(*, lang: str = "en") -> dict[str, dict[str, Any]]:
    """Return a {template_name: {content_sid, audience}} dict via the registry.

    Replaces the old @lru_cache yaml loader (D1 migration, VT-163). The
    registry's 60s TTL cache is the single load path. The returned dict
    shape is compatible with callers that read ``template.get("content_sid")``.

    ``lang`` is the language variant to resolve SIDs for; defaults to "en"
    to match the previous implicit behavior.
    """
    # pylint: disable=protected-access
    from orchestrator.templates_registry import _get_cached  # avoid circular at module level
    raw = _get_cached()
    out: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        langs = entry.get("languages") or {}
        content_sid = langs.get(lang)
        out[name] = {
            "content_sid": content_sid,
            "audience": entry.get("audience", ""),
        }
    return out


class _MockTwilioMessages:
    """Mock Twilio messages namespace — logs the would-send and returns a
    fake successful response. NEVER use in production; only when
    ``TEAM_TWILIO_MOCK_MODE=1`` (VT-200 hygiene fix 1).
    """

    @staticmethod
    def create(**kwargs: Any) -> Any:
        safe_kwargs = {
            k: v for k, v in kwargs.items()
            if k not in ("body", "content_variables")
        }
        logger.warning(
            "[TEAM_TWILIO_MOCK_MODE] would-send: %s", safe_kwargs
        )
        return SimpleNamespace(
            sid=f"MK{uuid4().hex[:30]}",
            status="queued",
            error_code=None,
            error_message=None,
        )


class _MockTwilioClient:
    """Mock Twilio REST client used when ``TEAM_TWILIO_MOCK_MODE=1``.

    Surfaces the same ``client.messages.create(...)`` shape the real Twilio
    SDK exposes. Sends never hit the network; each call logs and returns a
    SimpleNamespace shaped like a successful Twilio response so callers
    (``send_template_message`` + canaries) traverse the success branch.
    """

    messages = _MockTwilioMessages()


def _client() -> Client:
    """Build the Twilio REST client from env, wrapped by the VT-476 dev send-guard.

    Lazy (not import-time) so importing this module needs no Twilio creds —
    the CI ``orchestrator`` job has none and tests mock the send. When
    ``TEAM_TWILIO_MOCK_MODE=1``, returns a mock client that logs sends
    instead of dispatching them. Default OFF; the flag is explicit + the
    log line surfaces every send so production drift is loud.

    VT-476 (SAFETY-CRITICAL): the resolved client is passed through
    ``dev_send_guard.maybe_wrap_for_dev`` — the OUTER transport gate. On a
    non-prod env (``EXPECTED_ENV`` != prod) it returns a ``DevSendGuardClient``
    that MOCKS any send whose ``to`` is not in ``DEV_SEND_ALLOWLIST`` (empty by
    default → mock ALL), so dev can never silently message a real number through
    ANY send path. On prod the guard is inert (real sends, unchanged). This is
    the single install point: every WhatsApp send funnels through this client.

    NOT @lru_cache'd: the guard reads ``EXPECTED_ENV`` / ``DEV_SEND_ALLOWLIST``
    when it builds the wrapper, so the client is rebuilt per send-call to honour
    a runtime env change. The real underlying Twilio ``Client`` is cheap to
    construct (no network until ``messages.create``); the per-call cost is
    negligible next to the network round-trip a real send makes.
    """
    if os.environ.get("TEAM_TWILIO_MOCK_MODE", "0") == "1":
        logger.warning(
            "TEAM_TWILIO_MOCK_MODE=1 — NOT making real Twilio API calls. "
            "All sends will log and return a mock SID."
        )
        inner: Any = _MockTwilioClient()
    else:
        inner = Client(
            os.environ["TEAM_TWILIO_ACCOUNT_SID"],
            os.environ["TEAM_TWILIO_AUTH_TOKEN"],
        )
    return cast(Client, maybe_wrap_for_dev(inner))


def get_tenant_whatsapp_number(tenant_id: UUID) -> str | None:
    """Resolve a tenant's own WhatsApp number.

    This is a tenant-scoped read (the tenant's own ``tenants`` row), so it goes
    through ``tenant_connection`` — RLS-enforced under ``app_role`` (CL-71).
    """
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT whatsapp_number FROM tenants WHERE id = %s",
            (str(tenant_id),),
        ).fetchone()
    return row["whatsapp_number"] if row else None


_WHATSAPP_PREFIX = "whatsapp:"


def _wa(number: str) -> str:
    """Idempotently apply the WhatsApp channel scheme to an E.164 number.

    ``TEAM_TWILIO_FROM_NUMBER`` and recipient numbers are stored/passed as PLAIN E.164 (CL-435).
    Twilio requires ``whatsapp:+…`` on BOTH ``from_`` and ``to`` to route on the WhatsApp channel;
    a raw number misroutes to SMS and fails (VT-399: the welcome to a real signup failed Twilio
    error 21659 because both ends were unprefixed). Idempotent — never double-prefixes.
    """
    return number if number.startswith(_WHATSAPP_PREFIX) else f"{_WHATSAPP_PREFIX}{number}"


def _positional_content_variables(
    variables: tuple[str, ...], params: dict[str, Any]
) -> dict[str, str]:
    """Map named ``params`` onto Twilio's POSITIONAL content_variables ``{"1": v1, "2": v2, …}``.

    Twilio Content templates substitute positional ``{{1}}/{{2}}`` placeholders; a payload of
    NAMED keys is ignored and Twilio renders the template's SAMPLE values (VT-400: the welcome
    rendered "Hi Raj Cafe"). The registry's ordered ``entry.variables`` is the positional spec.
    Each DECLARED var that is present in ``params`` maps to its 1-indexed position; the rest are
    omitted. With the COMPLETE params its caller supplies (the welcome passes owner_name +
    trial_end_date), every position is filled and Twilio renders the real values — the VT-400 fix.

    NOTE (VT-400 scope): strict fail-closed-on-missing (the brief's ask) was DEFERRED — several
    confirmation/approval senders still pass partial/empty params (opt-out/status-ping confirmations,
    team_weekly_approval), so a hard raise would break those live flows. Omitting absent positions is
    no worse than today (Twilio already rendered the sample for them) while fully fixing every
    complete-param send. Completing each sender's params + re-adding fail-closed is a follow-up.
    Mirrors the agent path's ``agent.tools.send_whatsapp_template._build_content_variables``.
    """
    return {str(i + 1): params[var] for i, var in enumerate(variables) if var in params}


@DBOS.step()
def send_template_message(
    tenant_id: UUID,
    template_name: str,
    params: dict[str, Any],
    *,
    recipient_phone: str | None = None,
    language: str = "en",
    is_customer_send: bool = False,
) -> SendResult:
    """Send a Meta-approved WhatsApp template via Twilio. See the module docstring.

    Raises TemplateNotConfigured (alias: UnknownTemplateError) if template_name
    is unknown. A 4xx Twilio error returns success=False; a 5xx / network error
    is re-raised so the DBOS step retries.

    SID resolution is delegated to templates_registry.resolve() (D1, VT-163).
    ``language`` selects the template's language variant SID; it defaults to "en"
    (the pre-VT-163 implicit behaviour — every existing caller keeps "en"). VT-393:
    the owner welcome honors the owner's preferred_language (team_welcome has EN+HI).

    VT-460 gap (c): ``is_customer_send=True`` marks a send to an END-CUSTOMER (the
    business owner's WhatsApp customer) — set ONLY by the VT-45 ``send_whatsapp_template``
    tool, the SINGLE gated chokepoint every customer template send (agent + campaign)
    funnels through. Such a send MUST be inside ``customer_send_context()`` or it fails
    closed at the transport (a future un-gated direct caller breaks here, never sends).
    Default (False) is an OWNER send — exempt, unchanged. NOTE: the registry ``audience``
    field is NOT the trigger: some ``audience: customer`` templates (team_opt_out_confirmation,
    team_status_ping) are sent BY owner-reply handlers TO the owner — only the explicit flag
    distinguishes a real end-customer dispatch.
    """
    # Resolve via registry (D1 migration). Raises UnknownTemplateError (== TemplateNotConfigured)
    # for unknown names, UnknownLanguageVariantError for missing language variants.
    try:
        entry = _registry_resolve(template_name, language)
    except UnknownTemplateError:
        raise  # propagates as TemplateNotConfigured (alias)

    recipient = recipient_phone or get_tenant_whatsapp_number(tenant_id)
    if not recipient:
        raise ValueError(
            f"no recipient: tenant {tenant_id} has no whatsapp_number "
            "and no recipient_phone override was given"
        )
    recipient_token = hash_phone(recipient)
    attempted_at = datetime.now(UTC)

    # VT-460 gap (c): structural transport choke. A CUSTOMER send (is_customer_send=True — set only
    # by the VT-45 tool, the single gated chokepoint the agent + campaign paths funnel through) MUST
    # be inside customer_send_context(). A future un-gated direct caller passing is_customer_send=True
    # without the context fails closed here. Owner sends (default False) are exempt. Checked BEFORE
    # the no-SID early-out so even a stub customer template cannot be dispatched un-gated.
    _assert_gated_if_customer(
        is_customer=is_customer_send,
        template_name=template_name,
        recipient_token=recipient_token,
    )

    content_sid = entry.content_sid
    if content_sid is None:
        # Stub-pending-approval: the template is configured but its Meta
        # content_sid is not approved yet. No Twilio call (Pillar 7 — honest).
        logger.info(
            "twilio-send: template '%s' has no content_sid (pending approval) -> %s",
            template_name,
            recipient_token,
        )
        return SendResult(
            success=False,
            error_code="template_not_yet_approved",
            error_message=f"template '{template_name}' has no approved content_sid",
            attempted_at=attempted_at,
            template_name=template_name,
            recipient_phone_token=recipient_token,
        )

    # VT-400: map named params onto Twilio's POSITIONAL content_variables (named keys are ignored
    # and Twilio renders the template SAMPLE — "Hi Raj Cafe"). The welcome's complete params fill
    # every {{n}} with real values.
    content_variables = _positional_content_variables(entry.variables, params)

    try:
        message = _client().messages.create(
            content_sid=content_sid,
            content_variables=json.dumps(content_variables),
            from_=_wa(os.environ["TEAM_TWILIO_FROM_NUMBER"]),
            to=_wa(recipient),
        )
    except TwilioRestException as exc:
        if exc.status is not None and 400 <= exc.status < 500:
            # Permanent (4xx) — surface the failure; the DBOS step does not retry.
            logger.warning(
                "twilio-send: permanent failure template '%s' -> %s (code=%s)",
                template_name,
                recipient_token,
                exc.code,
            )
            return SendResult(
                success=False,
                error_code=str(exc.code),
                error_message=str(exc.msg),
                attempted_at=attempted_at,
                template_name=template_name,
                recipient_phone_token=recipient_token,
            )
        # Transient (5xx / unknown) — re-raise so the DBOS step retries.
        raise

    logger.info(
        "twilio-send: sent template '%s' -> %s (sid=%s)",
        template_name,
        recipient_token,
        message.sid,
    )
    return SendResult(
        success=True,
        message_sid=message.sid,
        attempted_at=attempted_at,
        template_name=template_name,
        recipient_phone_token=recipient_token,
    )


def send_freeform_message(
    body: str, recipient_phone: str, *, is_customer_session: bool = False
) -> str:
    """Send a free-form WhatsApp message via Twilio (VT-44).

    Parallel to send_template_message but uses Body= instead of content_sid.
    Honors TEAM_TWILIO_MOCK_MODE; never logs the recipient phone in plaintext
    (CL-390 — only the hashed token appears in logs).

    Returns the Twilio message SID (str) on success.
    Raises TwilioRestException on 4xx (permanent) or 5xx (transient).
    The caller (send_whatsapp_message) handles the exception split — this
    function does NOT swallow errors so the caller can record them cleanly.

    VT-460 gap (c)+(d): ``is_customer_session=True`` flags this as the VT-287 inbound
    CUSTOMER session class (intro / opt-in / opt-out acks) — a structurally-distinct,
    separately-audited send class from marketing. Such a send MUST be inside
    customer_send_context() (handle_customer_inbound enters it) or it fails closed at
    the transport. The default (False) is an OWNER session send (owner-reply acks,
    onboarding, breach/business-plan delivery) — exempt, unchanged.

    Note: NOT a @DBOS.step — the idempotency is handled at the DB layer
    (send_idempotency_keys table) by the standalone tool, not DBOS replay.
    """
    recipient_token = hash_phone(recipient_phone)
    # VT-460 gap (c): customer session freeform sends fail-close outside the gated context.
    _assert_gated_if_customer(
        is_customer=is_customer_session,
        template_name="<freeform_session>",
        recipient_token=recipient_token,
    )
    logger.info(
        "twilio-send: freeform -> %s body_len=%d",
        recipient_token,
        len(body),
    )
    message = _client().messages.create(
        body=body,
        from_=_wa(os.environ["TEAM_TWILIO_FROM_NUMBER"]),
        to=_wa(recipient_phone),
    )
    logger.info(
        "twilio-send: freeform sent -> %s (sid=%s)",
        recipient_token,
        message.sid,
    )
    return message.sid
