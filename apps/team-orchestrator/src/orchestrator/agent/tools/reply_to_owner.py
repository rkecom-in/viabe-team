"""VT-632 (Step 1) — ``reply_to_owner``: the manager brain's owner-reply AUTHORING tool.

The manager AUTHORS its owner-facing message by CALLING this tool, instead of writing trailing
message text that a downstream scrape ladder (the VT-589/593/616 lattice) has to guess at. Making
the reply a tool call means "did the brain reply" is a FACT (an assistant turn is recorded at the
transport chokepoint the moment the tool sends) rather than a heuristic reverse-scan, and it lets
the ``delegate() -> read result -> reply about it`` loop close in a single turn. This is the head-
of-distribution fix for the loop/stall + "I'm on it -> silence" trust-breakers (see
``.viabe/sprint/VT-632.md`` + ``.viabe/manager-objective.md``).

SECURITY (the VT-268 boundary — this is the risk-row):
  * This tool SENDS to the OWNER, but the recipient is resolved SERVER-SIDE from
    ``tenants.owner_phone`` (falling back to ``whatsapp_number``). The model NEVER supplies a phone
    number, so it can never target a customer. CUSTOMER sends stay forbidden — there is no such
    tool, and every campaign send still routes through the Pillar-7 approval gate. The tool NAME
    carries no ``FORBIDDEN_CAPABILITY_SUBSTRING`` (``tool_guardrail.py``), so it passes the fail-
    closed guard by construction; the guard's explicit owner-reply carve-out documents WHY.
  * Effect-boundary validation runs IN the tool BEFORE any send: PII redaction (the same
    ``redact_for_log`` primitive the internal audit path uses), near-duplicate reject-and-reask
    (no verbatim/semantic loop reaches the owner), a per-turn reply cap, and the #49 emission
    speech-act gate (a completion claim with no backing DB fact is swapped for an honest line).

Flag-gated: registered + instructed only when ``MANAGER_REPLY_TOOL`` is on (dev-first rollout;
the production cutover is Fazal-only). See ``orchestrator_agent.build_orchestrator_agent``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

logger = logging.getLogger("orchestrator.agent.reply_to_owner")

# Fazal-approved default (VT-632 open-question #4): a turn may land at most two owner bubbles
# (e.g. "here's the plan" + "want me to go ahead?"). A third is refused.
_PER_TURN_REPLY_CAP = 2

# Below this length a message is a bare affirmation ("haan", "done", "ok, thanks") that the owner
# may legitimately hear more than once — exempt it from the near-duplicate reject so a genuine short
# confirmation is never blocked (VT-632 design: "exempt very-short affirmations").
_DUP_EXEMPT_MAX_LEN = 24


def _resolve_owner_phone(tenant_id: UUID | str) -> str | None:
    """Owner recipient, resolved SERVER-SIDE: ``tenants.owner_phone`` falling back to
    ``whatsapp_number``. The model never supplies a number. Tenant-scoped read (RLS via
    ``tenant_connection``); mirrors ``campaign_outcome`` / ``task_outcome`` /
    ``request_owner_approval._resolve_owner_phone``. Best-effort — any error → None (the caller
    then errors the tool call rather than sending to an unknown recipient)."""
    try:
        from orchestrator.db.tenant_connection import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT owner_phone, whatsapp_number FROM tenants WHERE id = %s",
                (str(tenant_id),),
            ).fetchone()
    except Exception:
        logger.exception("reply_to_owner: owner-phone resolve failed tenant=%s", tenant_id)
        return None
    if not row:
        return None
    r = dict(row)
    return r.get("owner_phone") or r.get("whatsapp_number")


def _redact_owner_reply(tenant_id: UUID | str, text: str) -> str | None:
    """Owner-facing PII redaction (VT-632). Runs the pattern regexes (phone/email/PAN/Aadhaar/GST/
    CC) + the customer-name registry, but with ``hash_long_body=False`` so a real manager reply
    (routinely >200 chars) is NOT collapsed to a ``<body:hash:…>`` token (that whole-body hash
    bounds LOG size, not owner messages). FAIL-CLOSED: if redaction raises, returns empty, or leaves
    a residual body-hash token, return None so the caller REFUSES to send rather than shipping raw
    text or a garbage token. Customer NAMES are redacted via the tenant registry when it builds;
    a registry-build failure degrades to pattern-only (parity with the existing audit/collapse owner
    paths) — tightening that to a hard block is tracked as a VT-632 follow-up (HOLE 1)."""
    try:
        from orchestrator.agent.dispatch import _registry_for_tenant
        from orchestrator.privacy.pii_redactor import redact

        registry = _registry_for_tenant(tenant_id)
        out = redact(text, name_registry=registry, hash_long_body=False)
    except Exception:
        logger.exception("reply_to_owner: redaction raised tenant=%s — refusing to send", tenant_id)
        return None
    if not isinstance(out, str) or not out.strip():
        return None
    if out.startswith("<body:hash:"):
        logger.error(
            "reply_to_owner: redaction produced a body-hash token (tenant=%s) — refusing to send",
            tenant_id,
        )
        return None
    return out


def _count_prior_sends(messages: list[Any]) -> int:
    """Number of SUCCESSFUL ``reply_to_owner`` sends already made this run (per-turn cap). Counts
    only delivered replies (content begins ``sent``); refused attempts (dup/cap/empty) do not
    consume the cap, so a rejected dup can be retried with fresh text."""
    n = 0
    for m in messages:
        if getattr(m, "name", None) != "reply_to_owner":
            continue
        content = getattr(m, "content", "")
        if isinstance(content, str) and content.startswith("sent"):
            n += 1
    return n


@tool("reply_to_owner")
def reply_to_owner(text: str, state: Annotated[dict[str, Any], InjectedState]) -> str:
    """Send your reply to the business OWNER on WhatsApp. This is the ONLY way your words reach the
    owner — always end a handle-directly turn by calling it. Put the COMPLETE message in ``text``
    (the whole reply, in the owner's language). You do NOT pass a phone number; the runtime sends it
    to the owner. After you delegate() and read the result, call this to tell the owner what
    happened. NEVER claim an action ("done", "sent", "connected") unless a tool actually did it.

    Returns "sent" on success. If your text repeats a message you already sent, or you have already
    replied enough this turn, it returns an error — read it, then progress, delegate, or ask a
    specific question instead of repeating.
    """
    tenant_id = state.get("tenant_id")
    messages = state.get("messages", []) or []

    if tenant_id is None:
        return "error: no tenant context — cannot send. Escalate instead."

    body = (text or "").strip()
    if not body:
        return "error: empty text. Write the full message you want the owner to read."

    if _count_prior_sends(messages) >= _PER_TURN_REPLY_CAP:
        return (
            f"error: you already sent {_PER_TURN_REPLY_CAP} replies this turn. Stop replying — "
            "if there is more to do, delegate or take an action instead of sending another message."
        )

    # Effect boundary 1 — PII redaction at the send boundary (VT-632), FAIL-CLOSED. Owner-facing
    # redaction keeps the message text (no >200-char whole-body hash) but refuses to send on any
    # anomaly rather than shipping raw text or a hash token.
    redacted = _redact_owner_reply(tenant_id, body)
    if not redacted:
        return "error: could not safely prepare the message. Try again."
    body = redacted

    # Effect boundary 2 — near-duplicate reject-and-reask (no verbatim/semantic loop reaches the
    # owner). Short affirmations are exempt. Fail-soft ALLOW: a dup-check error must not block a send.
    if len(body) > _DUP_EXEMPT_MAX_LEN:
        try:
            from orchestrator.agent.dispatch import _reply_repeats_recent

            if _reply_repeats_recent(tenant_id, body):
                return (
                    "error: this repeats a message you already sent. Do NOT repeat it. Say "
                    "something NEW — give the next concrete step, delegate the work, or ask one "
                    "specific question."
                )
        except Exception:
            logger.debug("reply_to_owner: dup-check failed (fail-soft allow)", exc_info=True)

    # Effect boundary 3 — emission gate (#49): a completion claim ("done"/"bhej diya") with no
    # backing DB fact gets swapped for the honest line here, before it ever reaches the owner
    # (kills the fabrication as a CLASS, not just the one observed case).
    from orchestrator.agent.emission_gate import apply_emission_gate

    gated = apply_emission_gate(body, tenant_id)
    # R3 — did the gate SWAP the body (a completion/spend/debt claim it couldn't back)? If so we
    # still deliver the honest line to the owner, but the loop must NOT believe its own fabrication
    # reached them — return a corrective ``sent_adjusted`` result below so the brain does the work or
    # states the true status instead of re-claiming "done".
    gate_swapped = gated != body
    if gate_swapped:  # VT-640-DIAG (dev only; REMOVE after multi_field root-cause)
        logger.warning(
            "VT-640-DIAG reply_to_owner gate SWAPPED (tenant=%s) pre_gate=%r post=%r",
            tenant_id, body[:200], gated[:120],
        )
    body = gated

    # Recipient resolved SERVER-SIDE — the model never supplies a number.
    recipient = _resolve_owner_phone(tenant_id)
    if not recipient:
        return "error: could not resolve the owner's phone number. Escalate instead of sending."

    # send_freeform_ack records the assistant turn into conversation_log at the transport
    # chokepoint — that recorded turn is exactly what the dispatch scrape-gate + the D1 net read to
    # know the owner was already replied to (no double-send).
    from orchestrator.owner_surface.freeform_acks import send_freeform_ack

    sent = send_freeform_ack(tenant_id, recipient, body)
    if not sent:
        # Window-closed / send failure. Do NOT report success — leave the fallback net to handle it.
        return (
            "error: the message could not be delivered right now (the owner's chat window may be "
            "closed). Do not retry blindly."
        )

    logger.info("VT-632: reply_to_owner delivered (tenant=%s len=%d)", tenant_id, len(body))
    if gate_swapped:
        # R3 — the honest line WAS delivered (above), but the brain's original claim was a fabrication
        # with no matching DB record. Prefix is DELIBERATELY "sent" so ``_count_prior_sends`` still
        # counts this toward the per-turn cap (a swapped reply can't be retried into an infinite loop);
        # the rest tells the loop to progress rather than re-assert the false "done".
        return (
            "sent_adjusted: your reply claimed a completed action but NO matching DB record exists, "
            "so an honest status line was sent to the owner instead. Do the work now (delegate/act) "
            "or state the TRUE status — do not repeat the claim that it is done."
        )
    return "sent"


__all__ = ["reply_to_owner"]
