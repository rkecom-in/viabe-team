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
import re
import threading
import time
import unicodedata
from collections import OrderedDict, deque
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
from orchestrator.integrations.sender_resolution import (
    AUDIENCE_CUSTOMER,
    AUDIENCE_OWNER,
    SenderUnresolvable,
    resolve_sender,
)
from orchestrator.templates_registry import (
    UnknownTemplateError,
    resolve as _registry_resolve,
)
from orchestrator.utils.dev_send_guard import maybe_wrap_for_dev
from orchestrator.utils.phone_e164 import E164_RE
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
            # VT-676: media_url may be a SHORT-TTL signed PII-document URL (customer export) —
            # log its PRESENCE via media_count, never the URL itself.
            if k not in ("body", "content_variables", "media_url")
        }
        if "media_url" in kwargs:
            safe_kwargs["media_count"] = len(kwargs["media_url"] or ())
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


def get_tenant_whatsapp_number(tenant_id: UUID, *, conn: Any = None) -> str | None:
    """Resolve a tenant's own WhatsApp number — the OWNER-facing identity/recipient, never a sender
    (VT-742: the sender lives in ``tenant_whatsapp_accounts``; migration 050's "WABA sender" comment
    on this column is stale).

    This is a tenant-scoped read (the tenant's own ``tenants`` row), so it goes
    through ``tenant_connection`` — RLS-enforced under ``app_role`` (CL-71).

    ``conn`` (VT-742, mirroring ``wa_send_allowed``'s VT-460 contract): an already-open RLS-scoped
    tenant connection. Given one, the read rides it, so a caller needing BOTH the recipient and the
    sender identity for one send opens ONE connection instead of two.
    """
    query = "SELECT whatsapp_number FROM tenants WHERE id = %s"
    params = (str(tenant_id),)
    if conn is not None:
        row = conn.execute(query, params).fetchone()
    else:
        with tenant_connection(tenant_id) as own_conn:
            row = own_conn.execute(query, params).fetchone()
    if not row:
        return None
    return row["whatsapp_number"] if isinstance(row, dict) else row[0]


_WHATSAPP_PREFIX = "whatsapp:"

# VT-487: structural E.164 transport backstop. A WhatsApp recipient/sender MUST be a
# well-formed E.164 number: a leading '+', a non-zero country-code digit, then 7..14 more
# digits (8..15 total — the ITU E.164 max). This is the LAST line of defence against a
# malformed/corrupted number reaching Twilio — e.g. the float-corruption breach where a phone
# stored as a number rendered to scientific notation ("+91998886e+11"), which Twilio rejected
# with 21211 ("invalid To") on six live sends. Coercion at ingest (contacts._normalize_phone)
# is the primary fix; this guard makes it STRUCTURALLY impossible for a non-E.164 string to be
# dispatched even if a future ingest path slips one through.
#
# VT-742: the pattern itself now lives in ``utils.phone_e164`` because ``resolve_sender`` needs the
# same shape check one layer earlier (a sending number read out of the DATABASE must be refused at
# resolution, not discovered here). Two modules needing one rule is how a second, drifting regex
# gets written; the SHAPE is shared, the exception raised on a violation stays each caller's own.
_E164_RE = E164_RE


class BlockedRecipientError(ValueError):
    """Raised (fail-closed) when a send target is not a well-formed E.164 number (VT-487).

    The transport refuses to dispatch a malformed/corrupted number — a scientific-notation
    float artifact, an empty/garbage string, or anything that does not match
    ``^\\+[1-9]\\d{7,14}$``. PII-safe: the message carries only a hashed token + a last-4
    fragment, never the raw value (CL-390).
    """


def _assert_e164(number: str, *, role: str) -> None:
    """Fail-CLOSED E.164 structural guard (VT-487). ``role`` is 'recipient' or 'sender'.

    Strips an already-applied ``whatsapp:`` scheme before matching (idempotent with ``_wa``).
    Raises ``BlockedRecipientError`` — never sends — when the bare number is not valid E.164.
    The raised message is PII-safe: hashed token + last-4 only, never the plaintext number
    (CL-390). Catches the float-corruption artifact (``+91998886e+11`` contains 'e' → no match)
    and any other malformed target before it can reach ``messages.create``.
    """
    bare = number[len(_WHATSAPP_PREFIX):] if number.startswith(_WHATSAPP_PREFIX) else number
    if not _E164_RE.match(bare):
        token = hash_phone(bare) if bare else "<empty>"
        last4 = bare[-4:] if len(bare) >= 4 else "??"
        raise BlockedRecipientError(
            f"BLOCKED non-E.164 {role} at the transport (VT-487): {token} (..{last4}). "
            "A send target MUST match ^\\+[1-9]\\d{7,14}$; a malformed/corrupted number "
            "(e.g. a scientific-notation float artifact) is refused fail-closed before Twilio."
        )


def _wa(number: str, *, role: str = "recipient") -> str:
    """Validate (VT-487) then idempotently apply the WhatsApp channel scheme to an E.164 number.

    Sending numbers (from ``resolve_sender``) and recipient numbers are both stored/passed as PLAIN
    E.164 (CL-435).
    Twilio requires ``whatsapp:+…`` on BOTH ``from_`` and ``to`` to route on the WhatsApp channel;
    a raw number misroutes to SMS and fails (VT-399: the welcome to a real signup failed Twilio
    error 21659 because both ends were unprefixed). Idempotent — never double-prefixes.

    VT-487: BEFORE prefixing, the bare number is asserted to be well-formed E.164
    (``_assert_e164``). A malformed/corrupted target (scientific-notation float artifact, empty,
    garbage) raises ``BlockedRecipientError`` and is NEVER dispatched — the structural backstop so
    a corrupted number can never reach Twilio (the six 21211 "invalid To" failures in the log).
    """
    _assert_e164(number, role=role)
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


def _record_owner_conversation_turn(
    tenant_id: UUID | str | None, text: str, *, message_sid: str | None, surface: str
) -> None:
    """VT-579 — record an OWNER-facing send into the lifetime conversation log (the 'assistant' leg of the
    owner↔manager conversation).

    This is the single OUTBOUND chokepoint: the transport is the one funnel every owner send passes
    through, and the owner-vs-customer split is the existing is_customer_* flag the callers already set,
    so "record owner sends only" is a branch on a signal that lives right here. NO-OP without a tenant:
    ``send_template_message`` carries ``tenant_id`` natively; the tenant-blind freeform/interactive
    transports record only when an owner-facing caller supplies it — and the onboarding journey
    deliberately does NOT (it double-writes via ``_append_recent_turns``), so its turns are never
    double-logged here. Fail-soft: a send must NEVER fail because the memory write failed."""
    if tenant_id is None:
        return
    try:
        from orchestrator.conversation_log import record_turn

        record_turn(tenant_id, "assistant", text, message_sid=message_sid, surface=surface)
    except Exception:  # noqa: BLE001 — conversation memory is never a gate on a send
        logger.warning("twilio-send: conversation-log record failed (fail-soft)", exc_info=True)


# --- VT-683 P4: OWNER-audience template whitelist (SHADOW-first) --------------------------------
#
# The Fazal ruling (2026-07-18): the owner-facing TEMPLATE surface is MINIMAL — everything not
# whitelisted rides the 24h session (queued, idle-paced — VT-683 P2/P3). This is the transport-level
# enforcement of that whitelist, applied ONLY to OWNER-audience sends. A customer-audience template
# has its OWN structural choke (customer_send_context, VT-460 gap c) and is out of scope here — so
# applicability keys on the resolved registry ``audience`` field, NOT on ``is_customer_send``.
#
# MODE (TEAM_TEMPLATE_WHITELIST_ENFORCE, feature_flags — the house _on pattern):
#   unset/off = SHADOW — log a WARNING "template-whitelist SHADOW: <name> would be blocked" and send
#               normally (byte-identical to today). Shadow-first: surface what enforce would block.
#   on        = ENFORCE — refuse the send (a failed SendResult, error_code 'template_not_whitelisted';
#               never raises). Byte-identical behaviour when the template IS whitelisted.

OWNER_TEMPLATE_WHITELIST: frozenset[str] = frozenset(
    {
        # The three canonical whitelisted owner templates (welcome / wake-up / system callback).
        "team_welcome4",
        "team_wakeup2",
        "team_error_handler",
        # The approval templates that remain the OUT-OF-WINDOW belt until P2c's in-session interactive
        # ask + the P3 wake-up retire them. team_weekly_approval is audience:customer, so it is never
        # gated here anyway; listed for whitelist-intent completeness. team_agent_draft_approval is
        # audience:owner and IS gated — the whitelist keeps it sendable as the belt.
        "team_weekly_approval",
        "team_agent_draft_approval",
    }
)

#: SendResult.error_code returned when ENFORCE refuses a non-whitelisted owner template.
TEMPLATE_NOT_WHITELISTED = "template_not_whitelisted"

#: SendResult.error_code returned when VT-742's resolver cannot produce a sending number (no own
#: live WABA for a customer send, no valid pin, no valid default). Distinct from a Twilio failure
#: code — nothing was dispatched, and retrying will not make a sender appear.
SENDER_UNRESOLVABLE = "sender_unresolvable"


def _owner_template_whitelist_action(template_name: str, audience: str) -> str:
    """VT-683 P4 whitelist decision → ``'allow'`` | ``'shadow'`` | ``'block'``.

    Only an OWNER-audience template NOT in ``OWNER_TEMPLATE_WHITELIST`` is gated; a whitelisted owner
    template, OR any non-owner-audience (customer / blank) template, is ``'allow'``. For a gated
    template the ``TEAM_TEMPLATE_WHITELIST_ENFORCE`` flag decides: ``'block'`` when enforce is on,
    else ``'shadow'`` (log-only; send normally). Pure except for the single flag read — unit-tested
    directly (dep-less), which is why the whole decision lives in this small function."""
    if audience != "owner" or template_name in OWNER_TEMPLATE_WHITELIST:
        return "allow"
    from orchestrator.feature_flags import template_whitelist_enforce_enabled

    return "block" if template_whitelist_enforce_enabled() else "shadow"


# --- VT-718 S2: the single OWNER emission choke (CL-2026-07-28-single-voice-manager) ------------
#
# The Manager is ONE entity — it must never say the same thing twice with no owner turn between
# (the run-6 completion-boundary double-reply class). The transport is the single physical funnel
# every owner-bound send passes through (the only module that constructs the Twilio client), so
# the choke lives HERE: every existing and every FUTURE owner send is guarded automatically, with
# no caller migration and no bypassable wrapper. Mirrors the two sibling chokes above
# (customer_send_context, the VT-683 whitelist): shadow-first, suppress-only, fail-open.
#
# SUPPRESSION RULE — an owner-bound send is a duplicate iff a prior outbound to the SAME recipient
# within _EMISSION_WINDOW_S has the SAME normalized body AND no owner inbound arrived after that
# prior send. The inbound condition is what keeps legitimate verbatim re-asks alive: a re-present
# after an invalid answer always follows an owner turn; the double-reply disease is the Manager
# speaking twice into silence.
#
# Two layers: L1 = per-recipient in-process ring (catches the seconds-apart burst class, works
# tenant-blind, zero DB cost); L2 = conversation_log within-window check (cross-restart, only when
# the caller supplied a tenant). The choke can only SUPPRESS — never create, reorder, or approve a
# send; the customer choke and every effect gate upstream are untouched (ARCHITECTURE §0.1.1).
#
# TEMPLATE sends are deliberately NOT deduped: the whitelisted owner templates carry effectful
# asks (approvals), and two DISTINCT asks via the same template with no owner reply between are
# legitimate — suppressing an approval ask is the worst possible failure class for this guard.
# The double-reply disease lives in the freeform/interactive session voice; that is what's choked.

_EMISSION_WINDOW_S = 180.0  # matches the VT-716 typed-twice guard window
_EMISSION_RING = 8  # outbound bodies remembered per recipient
_EMISSION_CACHE_MAX = 512  # recipients tracked (LRU-bounded)

#: Sentinel SID returned by freeform/interactive when ENFORCE suppresses a duplicate. Truthy and
#: str so every caller's "sid = send_…" bookkeeping works unchanged; never a real Twilio SID.
CHOKE_SUPPRESSED_SID = "choke-suppressed"

#: SendResult.error_code when ENFORCE suppresses a duplicate template send (success stays True —
#: the conversation state is exactly as if the send landed, because its twin already did).
EMISSION_SUPPRESSED = "emission_suppressed"


class _RecipientEmissions:
    """Per-recipient outbound ring + the last-inbound marker. Process-local, lock-guarded."""

    __slots__ = ("outbound", "last_inbound_ts")

    def __init__(self) -> None:
        self.outbound: deque[tuple[float, str]] = deque(maxlen=_EMISSION_RING)
        self.last_inbound_ts: float = 0.0


_emission_lock = threading.Lock()
_emission_cache: OrderedDict[str, _RecipientEmissions] = OrderedDict()


def _emission_normalize(body: str) -> str:
    """The dedup identity of an outbound body: NFC, casefold, whitespace/punct collapsed, 200-char
    head — the VT-716 typed-twice normalizer shape, so both guards agree on what "the same" means."""
    norm = unicodedata.normalize("NFC", (body or "").strip().casefold())
    norm = norm.replace("'", "").replace("’", "").replace("‘", "")  # what's == whats (house pattern)
    norm = re.sub(r"[\s,.!?;:।/\\\-–—\"“”*_()]+", " ", norm).strip()
    return norm[:200]


def _emission_entry(recipient_token: str) -> _RecipientEmissions:
    """Fetch-or-create the LRU entry for a recipient. Caller holds ``_emission_lock``."""
    entry = _emission_cache.get(recipient_token)
    if entry is None:
        entry = _RecipientEmissions()
        _emission_cache[recipient_token] = entry
    _emission_cache.move_to_end(recipient_token)
    while len(_emission_cache) > _EMISSION_CACHE_MAX:
        _emission_cache.popitem(last=False)
    return entry


def note_owner_inbound(sender_phone: str) -> None:
    """Mark an owner INBOUND for the choke's no-inbound-between rule (called from the runner's
    early inbound record + the pre-tenant signup entry). Fail-soft, never raises."""
    try:
        if not sender_phone:
            return
        token = hash_phone(sender_phone)
        with _emission_lock:
            _emission_entry(token).last_inbound_ts = time.monotonic()
    except Exception:  # noqa: BLE001 — a marker miss only weakens dedup, never a send
        logger.debug("emission-choke: inbound marker failed (fail-soft)", exc_info=True)


def _l1_dupe(recipient_token: str, norm: str, now: float) -> bool:
    """L1 CHECK ONLY: same normalized body to the same recipient within the window, with no
    inbound after that prior send. Recording is separate (``_note_owner_emission``, post-success)
    so a FAILED attempt never poisons the ring — the fallback ladders (interactive → freeform with
    the same text) must always be free to resend what never actually went out."""
    with _emission_lock:
        entry = _emission_entry(recipient_token)
        return any(
            prior_norm == norm
            and (now - prior_ts) <= _EMISSION_WINDOW_S
            and entry.last_inbound_ts <= prior_ts
            for prior_ts, prior_norm in entry.outbound
        )


def _note_owner_emission(recipient_token: str, body: str) -> None:
    """Record a SUCCESSFUL owner send into the L1 ring. Called only after Twilio accepted the
    message (mode off records nothing — byte-identical). Fail-soft."""
    try:
        from orchestrator.feature_flags import owner_emission_choke_mode

        if owner_emission_choke_mode() == "off":
            return
        norm = _emission_normalize(body)
        if not norm:
            return
        with _emission_lock:
            _emission_entry(recipient_token).outbound.append((time.monotonic(), norm))
    except Exception:  # noqa: BLE001 — ring bookkeeping is never worth a send failure
        logger.debug("emission-choke: emission record failed (fail-soft)", exc_info=True)


def _l2_dupe(tenant_id: UUID | str, norm: str) -> bool:
    """L2: the same rule against conversation_log (cross-restart). Fail-OPEN — a memory read must
    never block a send."""
    try:
        from orchestrator.conversation_log import active_window

        turns = active_window(tenant_id, max_turns=12, max_age_h=1)
        now_utc = datetime.now(UTC)
        match_at = None
        for t in turns:  # chronological
            created = t.get("created_at")
            if created is None:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age = (now_utc - created).total_seconds()
            if t.get("role") == "owner":
                match_at = None  # an owner turn clears any earlier match — they spoke since
            elif (
                t.get("role") == "assistant"
                and 0 <= age <= _EMISSION_WINDOW_S
                and _emission_normalize(str(t.get("text") or "")) == norm
            ):
                match_at = created
        return match_at is not None
    except Exception:  # noqa: BLE001 — fail-open: dedup is advisory to the voice, never a gate
        logger.warning("emission-choke: L2 window read failed (fail-open)", exc_info=True)
        return False


def _owner_emission_guard(
    recipient_token: str, body: str, *, tenant_id: UUID | str | None, surface: str
) -> bool:
    """The S2 choke decision for one owner-bound send. Returns True iff the send must be
    SUPPRESSED (mode=enforce and the suppression rule matched). Shadow logs and sends normally.
    Off is byte-identical (nothing recorded, nothing read)."""
    from orchestrator.feature_flags import owner_emission_choke_mode

    mode = owner_emission_choke_mode()
    if mode == "off":
        return False
    norm = _emission_normalize(body)
    if not norm:
        return False
    dupe = _l1_dupe(recipient_token, norm, time.monotonic())
    if not dupe and tenant_id is not None:
        dupe = _l2_dupe(tenant_id, norm)
    if not dupe:
        return False
    if mode == "shadow":
        logger.warning(
            "emission-choke SHADOW: would suppress duplicate owner send -> %s surface=%s",
            recipient_token,
            surface,
        )
        return False
    logger.warning(
        "emission-choke ENFORCE: suppressed duplicate owner send -> %s surface=%s",
        recipient_token,
        surface,
    )
    return True


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

    # VT-683 P4: OWNER-template whitelist (SHADOW-first). Only owner-audience non-whitelisted sends
    # are gated; customer-audience templates route through their own choke above. Placed BEFORE the
    # no-SID early-out so ENFORCE refuses even a stub owner template that is off-whitelist. SHADOW
    # (default) is byte-identical — a WARNING then the normal send.
    _wl_action = _owner_template_whitelist_action(template_name, entry.audience)
    if _wl_action == "shadow":
        logger.warning("template-whitelist SHADOW: %s would be blocked", template_name)
    elif _wl_action == "block":
        logger.warning(
            "template-whitelist ENFORCE: refusing non-whitelisted owner template %s -> %s",
            template_name,
            recipient_token,
        )
        return SendResult(
            success=False,
            error_code=TEMPLATE_NOT_WHITELISTED,
            error_message=(
                f"owner template '{template_name}' is not in the VT-683 owner-template whitelist "
                "(everything else rides the 24h session)"
            ),
            attempted_at=attempted_at,
            template_name=template_name,
            recipient_phone_token=recipient_token,
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

    # VT-742 §1: the sender is RESOLVED per tenant, never read from a process-wide env constant at
    # the send site. Deliberately placed LAST, after the VT-460 ungated-customer-send rail and the
    # VT-683 whitelist: those are structural safety gates, and a fault raised before them would mask
    # which gate actually refused the send. Fail-closed here returns a failed SendResult rather than
    # raising — this is a @DBOS.step, a raise would be RETRIED, and a sender does not become
    # resolvable on a retry.
    try:
        sender = resolve_sender(tenant_id, audience=AUDIENCE_CUSTOMER if is_customer_send else AUDIENCE_OWNER)
    except SenderUnresolvable as exc:
        logger.error(
            "twilio-send: no sender resolved for tenant=%s template='%s' -> refusing the send: %s",
            tenant_id,
            template_name,
            exc,
        )
        return SendResult(
            success=False,
            error_code=SENDER_UNRESOLVABLE,
            error_message=str(exc),
            attempted_at=attempted_at,
            template_name=template_name,
            recipient_phone_token=recipient_token,
        )

    try:
        message = _client().messages.create(
            content_sid=content_sid,
            content_variables=json.dumps(content_variables),
            from_=_wa(sender.phone_number, role="sender"),
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
    # VT-579: record the OWNER-facing template send (the 'assistant' leg). CUSTOMER sends
    # (is_customer_send=True — the owner's own customers) are NOT this conversation; they live in
    # owner_message_audit, so they are excluded. The template body renders Twilio-side (not available
    # here), so record a compact, PII-safe marker — the template NAME only, never the params (which may
    # carry the owner's name).
    if not is_customer_send:
        _record_owner_conversation_turn(
            tenant_id, f"[template: {template_name}]", message_sid=message.sid, surface="system"
        )
    # VT-733B: meter the send. Twilio is the largest NON-LLM per-tenant cost and was never metered,
    # so every "what does this tenant cost us" number was an undercount. Recorded AFTER a confirmed
    # send (a failed send costs nothing) and fail-soft inside the meter itself — the message is
    # already out; a ledger write must never turn a delivered message into an error.
    _meter_template_send(tenant_id, message.sid)
    return SendResult(
        success=True,
        message_sid=message.sid,
        attempted_at=attempted_at,
        template_name=template_name,
        recipient_phone_token=recipient_token,
    )


def _meter_template_send(tenant_id: Any, message_sid: str | None) -> None:
    """VT-733B — record one Twilio template message on the integration cost ledger.

    Lazy import so this transport module stays import-light (and dep-less-smoke safe). The meter is
    itself fail-soft; this wrapper adds a second guard only so an import error in the metering module
    can never reach a delivered-message path.
    """
    try:
        from orchestrator.integrations.cost_meter import record_integration_cost

        record_integration_cost(
            tenant_id=tenant_id,
            vendor="twilio",
            unit="template_message",
            quantity=1,
            call_site="send_template_message",
            external_ref=message_sid,
        )
    except Exception:  # noqa: BLE001 — CL-122: metering never breaks a send
        logger.warning("VT-733B: template-send metering skipped", exc_info=True)


def send_typing_indicator(inbound_message_sid: str) -> None:
    """VT-697 (Fazal: 10s+ waits with no feedback) — fire the WhatsApp TYPING indicator for an
    inbound message. Twilio v3 Indicators endpoint (Public Beta): marks the referenced inbound
    as READ (blue ticks) and shows "typing…" until our reply lands or 25s, whichever first.

    Fire-and-forget on a daemon thread: the ingress hot path gains ZERO latency and NO failure
    mode — missing creds / mock mode / HTTP errors all reduce to a debug log. Never raises.
    The SDK has no v3 Indicators surface yet, so this posts directly.
    """
    sid = os.environ.get("TEAM_TWILIO_ACCOUNT_SID", "")
    tok = os.environ.get("TEAM_TWILIO_AUTH_TOKEN", "")
    if not sid or not tok or not inbound_message_sid:
        return
    if os.environ.get("TEAM_TWILIO_MOCK_MODE", "0") == "1":
        logger.info("[TEAM_TWILIO_MOCK_MODE] would-send typing indicator for %s", inbound_message_sid)
        return

    def _fire() -> None:
        try:
            import base64
            import urllib.request

            req = urllib.request.Request(
                "https://messaging.twilio.com/v3/Indicators/Typing.json",
                # channel MUST be uppercase — lowercase 400s (canary-proved 2026-07-23).
                data=json.dumps(
                    {"channel": "WHATSAPP", "messageId": inbound_message_sid}
                ).encode(),
                headers={
                    "Authorization": "Basic "
                    + base64.b64encode(f"{sid}:{tok}".encode()).decode(),
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:  # noqa: BLE001 — a typing indicator is never worth a failure
            logger.debug("typing indicator failed sid=%s (fail-soft)", inbound_message_sid)

    import threading

    threading.Thread(target=_fire, name="wa-typing-indicator", daemon=True).start()


def send_freeform_message(
    body: str,
    recipient_phone: str,
    *,
    is_customer_session: bool = False,
    tenant_id: UUID | str | None = None,
    surface: str = "manager",
    media_urls: list[str] | None = None,
    record_turn: bool = True,
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
    # VT-718 S2: the owner emission choke — suppress a duplicate owner send (enforce) rather than
    # double-speak. Returns a sentinel sid; never raises (a raise would trigger the callers'
    # freeform-fallback ladders and resend the very text being suppressed).
    if not is_customer_session and _owner_emission_guard(
        recipient_token, body, tenant_id=tenant_id, surface=surface
    ):
        return CHOKE_SUPPRESSED_SID
    logger.info(
        "twilio-send: freeform -> %s body_len=%d media=%d",
        recipient_token,
        len(body),
        len(media_urls or ()),
    )
    # VT-676: optional in-session media attachment (WhatsApp allows media on a freeform reply
    # inside an open session — no Meta media-template approval needed). The URL may be a SHORT-TTL
    # signed PII-document URL (customer export) — it is passed to the transport ONLY, never logged.
    # VT-742 §1: resolved per tenant. A customer session send REQUIRES the tenant's own live WABA —
    # a customer messaged from the shared number cannot reply to us at all, because customer inbound
    # resolves the tenant by the number the customer messaged TO (VT-742 finding 2). This raises
    # rather than returning: the callers' freeform-fallback ladders would otherwise re-attempt a send
    # that has no valid sender, and no send has happened.
    create_kwargs: dict[str, Any] = {
        "body": body,
        "from_": _wa(
            resolve_sender(
                tenant_id,
                audience=AUDIENCE_CUSTOMER if is_customer_session else AUDIENCE_OWNER,
            ).phone_number,
            role="sender",
        ),
        "to": _wa(recipient_phone),
    }
    if media_urls:
        create_kwargs["media_url"] = list(media_urls)
    # VT-676 fix-4f (canary r2 evidence: EVERY owner_notifications row ever written on dev is
    # stuck at 'accepted' — Twilio has NEVER posted us a delivery status): per-message
    # status_callback so failed/undelivered actually reaches the reconciliation ledger
    # (runner's status-callback leg → record_owner_notification_delivery). Env-gated + inert
    # when unset (today's behavior). The URL value is the Twilio-signed webhook (may carry the
    # Vercel bypass token) — never logged.
    _status_cb = os.environ.get("TEAM_TWILIO_STATUS_CALLBACK_URL")
    if _status_cb:
        create_kwargs["status_callback"] = _status_cb
    message = _client().messages.create(**create_kwargs)
    logger.info(
        "twilio-send: freeform sent -> %s (sid=%s)",
        recipient_token,
        message.sid,
    )
    if not is_customer_session:
        # VT-718: remember the successful send for the dedup ring (never the failed attempts —
        # the fallback ladders must stay free to resend what never went out).
        _note_owner_emission(recipient_token, body)
    # VT-579: record the OWNER freeform send (the 'assistant' leg) — verbatim body IS the conversation.
    # Only when the caller supplies a tenant; customer session sends (is_customer_session=True) are
    # excluded. VT-718: ``record_turn=False`` lets a caller with its OWN recorder (the journey
    # turn-brain path via _append_recent_turns) supply the tenant for dedup without double-logging.
    if not is_customer_session and record_turn:
        _record_owner_conversation_turn(tenant_id, body, message_sid=message.sid, surface=surface)
    return message.sid


def send_interactive_message(
    content_sid: str,
    recipient_phone: str,
    *,
    content_variables: dict[str, Any] | None = None,
    is_customer_session: bool = False,
    tenant_id: UUID | str | None = None,
    surface: str = "manager",
    record_turn: bool = True,
) -> str:
    """Send an interactive WhatsApp message (quick-reply buttons / list-picker / card) IN-SESSION (VT-479).

    Sends a pre-created Twilio Content object (an ``HX…`` content_sid) as a session message — the
    SAME ``messages.create(content_sid=…)`` mechanism ``send_template_message`` uses, but for the
    IN-WINDOW (≤24h) free-form path: an interactive content type sent inside the open customer-care
    window needs NO Meta template approval (twilio-quick-reply: ≤3 buttons in-session). The Content
    OBJECT must already exist (created once via the Content API; Twilio-side registration, not Meta
    whitelisting) — that's why the caller passes a content_sid, not inline buttons (``messages.create``
    has no inline-interactive parameter; interactive types are deliverable only via a Content object).

    Funnels through ``_client()`` like every other send, so the VT-476 dev send-guard AND
    ``TEAM_TWILIO_MOCK_MODE`` apply unchanged (a dev send to a non-allowlisted number is MOCKED;
    nothing escapes). ``is_customer_session`` gates a customer interactive send through the VT-460
    transport choke exactly as the freeform path does; the default (False) is an OWNER send
    (onboarding-journey questions) — exempt, unchanged.

    Returns the Twilio message SID. Raises TwilioRestException on a 4xx/5xx (no swallow — the caller
    decides the fallback; ``journey._send`` falls back to plain freeform text on any send failure).
    """
    recipient_token = hash_phone(recipient_phone)
    _assert_gated_if_customer(
        is_customer=is_customer_session,
        template_name="<interactive_session>",
        recipient_token=recipient_token,
    )
    # VT-718 S2: owner emission choke. The dedup identity is the visible prompt text (the "1"
    # variable — the journey/owner-question pattern); a text-less interactive keys on the content
    # object + its variables so an identical card double-fire is still caught.
    _dedup_body = str((content_variables or {}).get("1") or "") or (
        f"[content:{content_sid}]{json.dumps(content_variables or {}, sort_keys=True)}"
    )
    if not is_customer_session and _owner_emission_guard(
        recipient_token, _dedup_body, tenant_id=tenant_id, surface=surface
    ):
        return CHOKE_SUPPRESSED_SID
    # VT-742 §1: resolved per tenant (see send_freeform_message — same contract, same reasoning).
    create_kwargs: dict[str, Any] = {
        "content_sid": content_sid,
        "from_": _wa(
            resolve_sender(
                tenant_id,
                audience=AUDIENCE_CUSTOMER if is_customer_session else AUDIENCE_OWNER,
            ).phone_number,
            role="sender",
        ),
        "to": _wa(recipient_phone),
    }
    if content_variables:
        create_kwargs["content_variables"] = json.dumps(content_variables)
    logger.info(
        "twilio-send: interactive content_sid=%s -> %s",
        content_sid,
        recipient_token,
    )
    message = _client().messages.create(**create_kwargs)
    logger.info(
        "twilio-send: interactive sent -> %s (sid=%s)",
        recipient_token,
        message.sid,
    )
    if not is_customer_session:
        # VT-718: successful-send dedup record (same identity the guard checked).
        _note_owner_emission(recipient_token, _dedup_body)
    # VT-579: record the OWNER interactive send (the 'assistant' leg). The visible prompt text rides in
    # content_variables["1"] (the journey/owner-question pattern); record that. Only when a tenant is
    # supplied + it is an owner send (is_customer_session=False). VT-718: ``record_turn=False`` for
    # callers with their own recorder (journey), so they can supply the tenant without double-logging.
    if not is_customer_session and record_turn:
        _record_owner_conversation_turn(
            tenant_id,
            str((content_variables or {}).get("1") or ""),
            message_sid=message.sid,
            surface=surface,
        )
    return message.sid
