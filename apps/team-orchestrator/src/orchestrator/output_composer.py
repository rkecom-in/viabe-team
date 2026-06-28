"""Unified output composer (VT-30).

Deterministic Python that takes a specialist ``AgentResult`` + the current
``SubscriberState`` + an intent string and produces ONE
:class:`ComposedOutput` ready for ``send_template_message`` (template path)
or a future free-form send wrapper (free-form-24h path, downstream).

**ZERO LLM invocations.** The composer is in the scan scope of
``gate-no-llm-in-deterministic-triggers`` CI gate (extended to scan this
file's whole-file body per VT-30 review §Q3). Pillar 1 + Pillar 8
enforced structurally — the deterministic surface stays free of model
calls so the same inputs always produce the same outputs.

Surface
-------
- :class:`ComposedOutput` — frozen dataclass returned by the composer.
- :func:`compose_owner_output` — single entry point. Reads template
  routing + composes the message body per the deterministic rule
  precedence documented inline (24h-window → escalation framing →
  hard-limit explanation → template selection → honesty enforcement →
  language selection).
- :func:`load_template_routing` — yaml loader exposed for tests + the
  agent-tool wrapper.

Honesty rules (Pillar 7 owner-truth — Fazal-priority)
-----------------------------------------------------
1. No ARRR overstatement — uncertainty in attribution prefixes monetary
   mentions with ``"approximately"`` / ``"~"``.
2. No hidden failures — terminated ``AgentResult`` MUST surface the
   hard-limit axis in plain language.
3. No retention pressure — regex deny-list catches high-pressure copy.
4. No certainty claims about customer intent — composer frames
   inferred-intent specialist output as ``"pattern suggests"`` /
   ``"looks like"``.

These rules run as deterministic Python checks; tests cover each.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

from orchestrator import templates_registry
from orchestrator.templates_registry import (
    UnknownLanguageVariantError,
    UnknownTemplateError,
)


_ROUTING_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "config"
    / "template_routing.yaml"
)
_TEMPLATES_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "config"
    / "twilio_templates.yaml"
)


_TWENTY_FOUR_HOURS = timedelta(hours=24)


# Honesty rule #3 — pressure-phrase deny-list. Lowercase regex; ``re.IGNORECASE``
# applied at match time. Brief examples + 4 additional from Pillar 7 in
# ``concept-team-pillars.md``.
_PRESSURE_DENY = re.compile(
    r"(\bare you sure\?.*look at\b|"
    r"\bbut you\b|"
    r"\bdon[''']t leave\b|"
    r"\bone last chance\b|"
    r"\bwait[!.]+\b|"
    r"\bplease\s+don[''']t\s+go\b|"
    r"\byou'?re\s+missing\s+out\b|"
    r"\bbig\s+mistake\b)",
    re.IGNORECASE,
)

# Honesty rule #4 — certainty-claim regex. The composer transforms these
# to softer "pattern suggests" framing when inferred intent is present.
_CERTAINTY_PHRASES = re.compile(
    r"\b(customer wants|owner wants|user wants|user needs)\b",
    re.IGNORECASE,
)


MessageType = Literal["free_form_24h", "template"]
Urgency = Literal["low", "medium", "high", "critical"]
PreferredLanguage = Literal["en", "hi"]


@dataclass(frozen=True)
class ComposedOutput:
    """Result of :func:`compose_owner_output`. Send-ready envelope.

    ``signature`` is a deterministic SHA-256 hash of the canonical
    representation of the composed body. VT-125 wires the future
    ``send_whatsapp_message`` / ``send_whatsapp_template`` tools to
    verify this signature on dispatch, ensuring agent-path messages
    flow through the composer.
    """

    message_body: str
    message_type: MessageType
    template_name: str | None
    template_params: dict[str, str] = field(default_factory=dict)
    urgency: Urgency = "low"
    follow_up_required: bool = False
    follow_up_intent: str | None = None
    preferred_language: PreferredLanguage = "en"
    signature: str = ""
    honesty_notes: list[str] = field(default_factory=list)


def load_template_routing(path: Path | None = None) -> dict[str, Any]:
    """Parse ``template_routing.yaml`` into the routing table dict.

    Cached on disk path; safe to call repeatedly.
    """
    p = path or _ROUTING_PATH
    if not p.exists():
        raise FileNotFoundError(f"template_routing.yaml not found at {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"template_routing.yaml must be a mapping; got {type(data).__name__}")
    return data


def load_twilio_templates(path: Path | None = None) -> dict[str, Any]:
    """Return the raw name → yaml-record dict from twilio_templates.yaml.

    Delegates to templates_registry's internal loader so the registry's
    60s TTL cache is the single load path (D1 — one source of truth).

    Kept for backward-compat with tests that assert routing names are
    present in the template map. The routing test uses the returned dict
    keys only (not the ``content_sid`` flat field), so the nested-language
    yaml shape is transparent here.
    """
    # pylint: disable=protected-access
    return templates_registry._load_raw(path or _TEMPLATES_PATH)


def _tenant_preferred_language(state: Any) -> PreferredLanguage:
    """Resolve the tenant's preferred language — PER-TENANT, not global (VT-416 PR-3).

    Reads ``state['preferred_language']`` when present and valid (a Hindi-preference
    owner therefore gets the Hindi template variant). Falls back to the global
    ``TENANT_DEFAULT_LANGUAGE`` env default (``"en"``) ONLY when the state key is
    absent, empty, or an unrecognised value — so the path stays safe even on prod,
    where the ``tenants.preferred_language`` column may not yet be threaded into
    every state (the needs-triage item).

    Prior to this fix the function ignored ``state`` entirely and returned ONE
    global language for EVERY tenant, so a Hindi-preference owner silently got
    English template variants.
    """
    # Per-tenant: prefer the value carried on state.
    raw_state = state.get("preferred_language") if hasattr(state, "get") else None
    if raw_state:
        candidate = str(raw_state).lower()
        if candidate in ("en", "hi"):
            return candidate  # type: ignore[return-value]

    # Fallback: global default (column not yet populated / no state value).
    raw = os.environ.get("TENANT_DEFAULT_LANGUAGE", "en").lower()
    if raw not in ("en", "hi"):
        return "en"
    return raw  # type: ignore[return-value]


def _resolve_template(
    intent_or_trigger: str,
    phase: str,
    routing: dict[str, Any],
    templates: dict[str, Any],
    language: str = "en",
) -> tuple[str | None, str | None]:
    """Look up ``(intent, phase)`` → ``(template_name, content_sid)``.

    Falls through to the ``any`` phase key when the specific phase isn't
    listed. Returns ``(None, None)`` when no template applies (caller
    routes to free-form path).

    SID resolution is delegated to ``templates_registry.resolve()`` so the
    single yaml load path and 60s TTL cache apply (D1 migration, VT-163).
    The ``templates`` dict is used only for routing-name existence checks
    (back-compat with the test seam that injects custom template dicts).
    When ``templates`` is the real on-disk data (nested-lang shape), we fall
    through to registry resolution; when it's a test-injected dict, we
    attempt a best-effort flat ``content_sid`` read.
    """
    bucket = routing.get(intent_or_trigger)
    if bucket is None:
        return None, None
    template_name = bucket.get(phase) or bucket.get("any")
    if not template_name:
        return None, None

    # Prefer registry resolution (real path) — catches language-keyed SIDs.
    try:
        entry = templates_registry.resolve(template_name, language)
        return template_name, entry.content_sid
    except (UnknownTemplateError, UnknownLanguageVariantError):
        pass

    # Fallback: test-injected dict may use flat {content_sid: ...} shape.
    sid_row = templates.get(template_name)
    if sid_row is None:
        return template_name, None
    return template_name, sid_row.get("content_sid")


def _within_24h_window(last_owner_message_at: datetime | None, now: datetime) -> bool:
    """24-hour-window predicate. ``None`` (never messaged) → outside window."""
    if last_owner_message_at is None:
        return False
    return now - last_owner_message_at <= _TWENTY_FOUR_HOURS


def _signature(body: str, message_type: str, template_name: str | None) -> str:
    """Deterministic SHA-256 hash of the composer's canonical envelope.

    Future hash-signature gate (VT-125) verifies dispatched messages
    against this signature.
    """
    payload = f"{message_type}|{template_name or '-'}|{body}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Honesty rule helpers
# ---------------------------------------------------------------------------

def _enforce_no_arrr_overstatement(body: str, specialist_result: Any) -> tuple[str, list[str]]:
    """Honesty rule #1. Returns ``(body, notes)``.

    If specialist_result carries ``attribution_uncertain: True`` AND the
    body mentions a paise/rupee amount, prefix the amount with
    ``"approximately"`` (or ``"~"`` for shorter form).
    """
    notes: list[str] = []
    out = (specialist_result.output if specialist_result else None) or {}
    if not isinstance(out, dict):
        return body, notes
    if not out.get("attribution_uncertain"):
        return body, notes

    # Match ₹NNNN or NNNN rupees / paise references. Prefix with "approximately".
    new_body, n = re.subn(
        r"(₹\s?\d[\d,]*)",
        r"approximately \1",
        body,
        count=1,
    )
    if n > 0:
        notes.append("arrr_uncertainty_prefix_applied")
        return new_body, notes
    return body, notes


def _enforce_no_certainty_claims(body: str, specialist_result: Any) -> tuple[str, list[str]]:
    """Honesty rule #4. Replace ``"customer wants X"`` → ``"pattern suggests X"``."""
    notes: list[str] = []
    out = (specialist_result.output if specialist_result else None) or {}
    is_inferred = isinstance(out, dict) and out.get("intent_inferred") is True
    if not is_inferred:
        return body, notes
    new_body, n = _CERTAINTY_PHRASES.subn("pattern suggests", body)
    if n > 0:
        notes.append(f"certainty_claim_soft_landed:{n}")
        return new_body, notes
    return body, notes


def _check_pressure(body: str) -> list[str]:
    """Honesty rule #3. Returns list of matched pressure phrases (notes)."""
    matches = _PRESSURE_DENY.findall(body)
    if matches:
        return [f"pressure_phrase_detected:{m}" for m in matches]
    return []


def _prepend_escalation_framing(
    body: str, state: Any, urgency: Urgency
) -> tuple[str, Urgency, list[str]]:
    """Honesty rule #2 (a). Escalation_pending → prepend honest framing."""
    notes: list[str] = []
    if not getattr(state, "get", lambda *_: None)("escalation_pending"):
        return body, urgency, notes
    framed = (
        "The agent encountered an issue; here's what happened: " + body.lstrip()
    )
    notes.append("escalation_framing_prepended")
    return framed, "high", notes


def _explain_hard_limit(
    body: str, specialist_result: Any
) -> tuple[str, list[str]]:
    """Honesty rule #2 (b). Terminated specialist → identify axis in plain language."""
    notes: list[str] = []
    if specialist_result is None:
        return body, notes
    status = getattr(specialist_result, "status", None)
    if status != "terminated":
        return body, notes
    axis = getattr(specialist_result, "terminated_by", None)
    axis_phrases = {
        "tokens": "the response budget was reached",
        "tool_calls": "the per-run tool-call budget was reached",
        "depth": "the reasoning-depth budget was reached",
        "wallclock_ms": "the per-run time budget was reached",
        "cost_paise": "the per-run ₹50 cost budget was reached",
    }
    phrase = axis_phrases.get(str(axis), "a hard limit was reached")
    framed = (
        f"The agent encountered an issue ({phrase}); here's what we have so far: "
        + body.lstrip()
    )
    notes.append(f"hard_limit_axis_explained:{axis}")
    return framed, notes


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compose_owner_output(
    specialist_result: Any | None,
    state: Any,
    intent_or_trigger: str,
    *,
    now: datetime | None = None,
    routing: dict[str, Any] | None = None,
    templates: dict[str, Any] | None = None,
) -> ComposedOutput:
    """Compose ONE owner-facing message envelope from specialist + state + intent.

    Deterministic Python. Same inputs → same output. Tests assert
    byte-identical message bodies for fixed fixtures.

    Parameters
    ----------
    specialist_result : ``AgentResult`` | None
        Specialist's structured envelope. ``None`` valid for
        deterministic triggers (e.g. attribution-close confirmation).
    state : ``SubscriberState`` (dict-like access)
        Tenant's current orchestrator state. Reads ``phase``,
        ``escalation_pending``, ``last_owner_message_at``.
    intent_or_trigger : str
        Routing key — maps via ``template_routing.yaml`` to a
        ``template_name``, which maps via ``twilio_templates.yaml`` to a
        ``content_sid``.
    now : datetime | None
        UTC clock injection (testability). Defaults to ``datetime.now(UTC)``.
    routing : dict[str, Any] | None
        Override for routing yaml content (testability). Defaults to
        on-disk ``template_routing.yaml``.
    templates : dict[str, Any] | None
        Override for templates yaml content (testability). Defaults to
        on-disk ``twilio_templates.yaml``.
    """
    now = now or datetime.now(timezone.utc)
    routing = routing if routing is not None else load_template_routing()
    templates = templates if templates is not None else load_twilio_templates()

    state_phase = (state.get("phase") if hasattr(state, "get") else None) or "unknown"
    last_owner = state.get("last_owner_message_at") if hasattr(state, "get") else None
    within_window = _within_24h_window(last_owner, now)

    # Template-name lookup.
    template_name, content_sid = _resolve_template(
        intent_or_trigger, state_phase, routing, templates
    )

    # Outside-24h → MUST use a template. If no template applies, fall back
    # to the catch-all `unable_to_complete_request` template (Tier-A).
    if not within_window and template_name is None:
        template_name = "team_unable_to_complete_request"
        _, content_sid = _resolve_template("unable_to_complete", "any", routing, templates)
        # Hardcoded direct lookup via registry as last resort (D1 migration).
        if content_sid is None:
            try:
                content_sid = templates_registry.resolve(template_name, "en").content_sid
            except (UnknownTemplateError, UnknownLanguageVariantError):
                # Test-injected dict fallback (flat content_sid shape).
                if template_name in templates:
                    content_sid = templates[template_name].get("content_sid")

    # Build the message body. Template path keeps the body short (variable
    # substitution lives in template_params); free-form path uses
    # specialist content + framing.
    notes: list[str] = []
    if template_name is not None and content_sid is not None:
        message_type: MessageType = "template"
        # Body is the human-readable form of what the template will say
        # (the actual content lives in Twilio Console + Meta WABA per
        # Pillar 8). The body string here is for logging / tests.
        message_body = f"<{template_name}>"
        template_params = _derive_template_params(intent_or_trigger, specialist_result, state)
    else:
        message_type = "free_form_24h"
        message_body = _derive_free_form_body(intent_or_trigger, specialist_result, state)
        template_params = {}

    urgency: Urgency = _derive_urgency(specialist_result, state)

    # Honesty enforcement pipeline.
    message_body, n0 = _enforce_no_arrr_overstatement(message_body, specialist_result)
    notes.extend(n0)
    message_body, n1 = _enforce_no_certainty_claims(message_body, specialist_result)
    notes.extend(n1)
    message_body, n2 = _explain_hard_limit(message_body, specialist_result)
    notes.extend(n2)
    message_body, urgency, n3 = _prepend_escalation_framing(
        message_body, state, urgency
    )
    notes.extend(n3)

    # Pressure check is enforce-fail style: detected pressure phrases get
    # surfaced in honesty_notes; the composer does NOT silently rewrite.
    # Tests + Fazal-personal review at pre-merge catch any drift.
    notes.extend(_check_pressure(message_body))

    sig = _signature(message_body, message_type, template_name)
    follow_up_required, follow_up_intent = _derive_follow_up(intent_or_trigger, specialist_result, state)

    return ComposedOutput(
        message_body=message_body,
        message_type=message_type,
        template_name=template_name,
        template_params=template_params,
        urgency=urgency,
        follow_up_required=follow_up_required,
        follow_up_intent=follow_up_intent,
        preferred_language=_tenant_preferred_language(state),
        signature=sig,
        honesty_notes=notes,
    )


# ---------------------------------------------------------------------------
# Internal — body / param / urgency / follow-up derivations
# ---------------------------------------------------------------------------

def _owner_name(state: Any) -> str:
    """Best-effort owner display name from state; "" when unavailable.

    Phase 1 has no wired owner-name source (the column lands later), so this
    returns "" — the {{1}} slot renders empty rather than crashing the send
    contract (validate_params checks param KEYS, not values).
    """
    name = state.get("owner_name") if hasattr(state, "get") else None
    return str(name) if name else ""


def _derive_template_params(
    intent_or_trigger: str, specialist_result: Any, state: Any
) -> dict[str, str]:
    """Build the variable-substitution map for the template send."""
    params: dict[str, str] = {}
    out = (specialist_result.output if specialist_result else None) or {}
    if not isinstance(out, dict):
        out = {}

    # VT-248: the fail-closed campaign-rejection surface. The owner is told the
    # COUNT of targets that couldn't be verified — never ids, never a
    # cross-tenant distinction (VT-241 privacy invariant; the full rejected-id
    # list stays in the operator audit log). rejected_count is threaded from
    # the campaign_rejected terminal dict via dispatch._classify_terminal.
    if intent_or_trigger == "campaign_not_sent_invalid_cohort":
        params["owner_name"] = _owner_name(state)
        params["unverified_count"] = str(int(out.get("rejected_count", 0)))
        return params

    # VT-486: the out-of-window owner re-engagement template carries a single var, {{1}}=owner_name.
    if intent_or_trigger == "reengage":
        params["owner_name"] = _owner_name(state)
        return params

    if isinstance(out, dict):
        for key in ("customer_name", "amount_paise", "campaign_name", "month"):
            if key in out:
                params[key] = str(out[key])
    return params


def _derive_free_form_body(
    intent_or_trigger: str, specialist_result: Any, state: Any
) -> str:
    """Compose a free-form body from specialist content + orchestrator framing.

    Phase-1 minimal: returns the specialist's ``output.message`` field if
    present, otherwise a sentinel describing the intent. The detailed
    free-form copy machinery (templates with slot substitution) ships
    downstream.
    """
    out = (specialist_result.output if specialist_result else None) or {}
    if isinstance(out, dict) and isinstance(out.get("message"), str):
        return out["message"]
    return f"[{intent_or_trigger}] no template applies; specialist provided no message"


def _derive_urgency(specialist_result: Any, state: Any) -> Urgency:
    """Urgency floor: terminated + escalation_pending raise to high/critical."""
    if specialist_result and getattr(specialist_result, "status", None) == "terminated":
        return "high"
    if hasattr(state, "get") and state.get("escalation_pending"):
        return "high"
    return "low"


def _derive_follow_up(
    intent_or_trigger: str, specialist_result: Any, state: Any
) -> tuple[bool, str | None]:
    """Heuristic follow-up routing. Conservative: only trigger when explicit."""
    out = (specialist_result.output if specialist_result else None) or {}
    if isinstance(out, dict) and out.get("follow_up_required"):
        return True, str(out.get("follow_up_intent") or "unspecified")
    return False, None


__all__ = [
    "ComposedOutput",
    "MessageType",
    "PreferredLanguage",
    "Urgency",
    "compose_owner_output",
    "load_template_routing",
    "load_twilio_templates",
]
