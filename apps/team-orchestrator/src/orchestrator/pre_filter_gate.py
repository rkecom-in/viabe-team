"""Pre-Filter Gate — Stage 1 of the two-stage filter (VT-3.8).

FULLY DETERMINISTIC (Pillar 1, revised 2026-05-12): regex / exact-match /
signature checks only. ZERO LLM calls — CI greps this file and direct_handlers/
to enforce it.

Conservative matching: a false route to the brain (a cost overhead) is
preferred over a false route to a direct handler (a privacy / UX risk). When
in doubt, route to the brain.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from dbos import DBOS

from orchestrator.keyword_match import boundary_patterns, nfc as _nfc
from orchestrator.state import SubscriberState
from orchestrator.types import (
    PreFilterResult,
    RouteToBrain,
    RouteToDirectHandler,
    WebhookEvent,
)

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _load_keywords(filename: str) -> list[str]:
    data = yaml.safe_load((_CONFIG_DIR / filename).read_text())
    return [str(keyword) for keyword in data.get("keywords", [])]


def _boundary_patterns(filename: str) -> list[re.Pattern[str]]:
    """VT-329/VT-358: boundary-safe CONTAINMENT patterns from a keyword file, via the shared
    `keyword_match` helper — the owner gate (here) + the customer opt-out path
    (`integrations.customer_inbound`) compile from the SAME helper so they can't drift. `\\b` is
    dead for Devanagari matras; see keyword_match for the rationale + the stem-through-matra
    fail-safe over-match."""
    return boundary_patterns(_load_keywords(filename))


# Opt-out: VT-329 — boundary-safe CONTAINMENT (was whole-body-exact, which missed "please बंद
# करो" / danda variants). Same lookaround approach as DSR. Failure direction is DPDP-safe.
_OPT_OUT_PATTERNS = _boundary_patterns("opt_out_keywords.yaml")

# VT-303 — data-inputs ENABLE (opt-in / consent-grant): exact match (case-insensitive, NFC) on
# the whole trimmed body, the inverse of opt-out. Routes to data_inputs_enable_handler.
_ENABLE_KEYWORDS = {_nfc(kw.casefold()) for kw in _load_keywords("data_inputs_enable_keywords.yaml")}

# DSR: boundary-safe containment (see _boundary_patterns).
_DSR_PATTERNS = _boundary_patterns("dsr_keywords.yaml")

# VT-384 (Gap-5 PR-3) — L3 autonomy keyword sets (config/l3_keywords.yaml), LOCKSTEP with the
# CL-438 Meta-approved team_autonomy_offer body. BOTH rules are ordered strictly AFTER the
# authoritative opt-out + DSR rules (the CL-438 floor — see RULE_ORDER below). The `kill` set is
# boundary-safe CONTAINMENT (the opt-out style) of AUTONOMY-SPECIFIC kill phrases; the `enable` set
# is EXACT whole-body match (the data-inputs ENABLE style). Bare STOP stays the authoritative
# opt-out path (opt_out_keywords.yaml) and wins first — it is NOT duplicated in the kill set.
def _load_l3_keyword_section(section: str) -> list[str]:
    data = yaml.safe_load((_CONFIG_DIR / "l3_keywords.yaml").read_text())
    return [str(keyword) for keyword in (data.get(section) or [])]


_L3_KILL_PATTERNS = boundary_patterns(_load_l3_keyword_section("kill"))
_L3_ENABLE_KEYWORDS = {_nfc(kw.casefold()) for kw in _load_l3_keyword_section("enable")}

# VT-384 condition C-b — the RULE-ORDER PIN (structural, not just behavioral). This is the
# AUTHORITATIVE source-of-truth ordering of the inbound-body rules in :func:`pre_filter`, kept in
# the SAME order they execute. The acceptance suite asserts ``opt_out`` and ``dsr`` both precede
# ``l3_kill`` and ``l3_enable`` here (CL-438 floor: the authoritative DPDP matchers run first), so a
# future rule insertion that silently reorders the gate fails the pin test instead of shipping a
# compliance regression. Any change to the rule sequence in pre_filter MUST update this list in
# lockstep (the pin test reads THIS list, then the rules read in the same sequence).
INBOUND_BODY_RULE_ORDER: tuple[str, ...] = (
    "opt_out",     # Rule a  — authoritative DPDP opt-out (FIRST; CL-438 floor)
    "data_inputs_enable",  # Rule a2 — VT-303 owner_inputs consent grant
    "dsr",         # Rule b  — authoritative DPDP data-subject request
    "l3_kill",     # Rule b2 — VT-384 autonomy kill (AFTER opt-out + DSR)
    "l3_enable",   # Rule b3 — VT-384 autonomy ENABLE (AFTER opt-out + DSR)
    "status_ping", # Rule f
    "integration_intent",  # Rule g
    "brain",       # Rule h — fallthrough
)
# Alias the C-b acceptance suite's declarative-list pin reads by index (test_vt384_pre_filter_
# rule_order.test_declarative_rule_list_order_if_present) — the strongest form of the order pin.
_RULE_ORDER = INBOUND_BODY_RULE_ORDER

# Status ping: narrow regex — matches ONLY a whole-message status QUERY.
#
# VT-464 D2: bare greetings (hi/hello/hey/namaste) were REMOVED from this set.
# A standalone greeting must fall THROUGH to the brain (Rule h) so the rebuilt
# Team-Manager greets + onboards the owner as a business manager — the prior
# regex swallowed "Hi"/"Hello"/"Hey" into status_ping_handler before the brain
# ever ran, bypassing the "Hi → business-manager, not customer-service" fix.
# Only genuine STATUS-intent phrases ("any update", "what's the status",
# "kya hua", "कैसा चल रहा है") route to status_ping_handler. This is
# DPDP-adjacent routing: it does NOT touch opt-out / DSR / consent rules.
_STATUS_PING = re.compile(
    r"^\s*("
    r"any update|any updates|"
    r"(what'?s|whats)\s+(the\s+)?status|status\s+update|"
    r"kya\s+hua|kya\s+update|"
    r"कैसा चल रहा है"
    r")\s*[?!.]*\s*$",
    re.IGNORECASE,
)

# VT-206 Q4 — integration intent classifier. Precise regex; biases
# toward false-negative per Cowork flag (ambiguous "use" phrases must
# NOT match — fall through to brain for classification). Two patterns
# UNION'd: (a) verb + integration-noun, (b) generic onboarding phrases.
_INTEGRATION_INTENT_RE = re.compile(
    r"\b("
    # (a) verb + integration noun / connector name
    r"(add|connect|setup|set\s*up|configure|integrate)\s+(my\s+|the\s+)?"
    r"(integration|shopify|sheet|spreadsheet|crm|gohighlevel|woocommerce|"
    r"google\s*analytics|ga4|amazon|razorpay|meta\s*ads|pixel|connector|data\s*source)"
    r"|"
    # (b) generic onboarding phrases
    r"onboard(ing)?|set\s*me\s*up|connect\s*my\s*data|i\s+want\s+to\s+use\s+"
    r"(shopify|sheet|spreadsheet|crm|gohighlevel|woocommerce|"
    r"google\s*analytics|ga4|amazon|razorpay|meta\s*ads)"
    r")\b",
    re.IGNORECASE,
)


def _normalize(body: str) -> str:
    """Collapse whitespace, case-fold, NFC-normalize for exact keyword comparison (the enable
    gate). VT-329: NFC so a decomposed Devanagari body matches the canonical keyword form."""
    return _nfc(" ".join(body.split()).casefold())


def matches_opt_out_or_dsr(body: str) -> bool:
    """True if ``body`` CONTAINS an opt-out or DSR keyword (VT-329: boundary-safe containment, NFC,
    EN+Devanagari+Hinglish). Phase-aware reply gates call this so opt-out / DSR routing ALWAYS wins
    over any other interpretation (DPDP): a tenant who says "delete my data" / "बंद करो" /
    "band karo" must reach the dsr/opt-out handler regardless of phase."""
    nfc = _nfc(body)
    return any(p.search(nfc) for p in _OPT_OUT_PATTERNS) or any(p.search(nfc) for p in _DSR_PATTERNS)


def matches_kill_keyword(body: str) -> bool:
    """True if ``body`` CONTAINS an L3 autonomy-kill phrase (config/l3_keywords.yaml `kill` set;
    NFC, boundary-safe containment — the same matcher pre_filter rule b2 runs). The runner
    owner-inbound demote leg calls this to EXCLUDE a kill keyword: a kill must FREEZE via
    autonomy_kill_handler (cancel holds/batches outright), not merely DEMOTE-and-regress. Bare
    opt-out keywords are deliberately absent from the kill set (they route to opt_out_handler
    first), so this never overlaps matches_opt_out_or_dsr."""
    nfc = _nfc(body)
    return any(p.search(nfc) for p in _L3_KILL_PATTERNS)


@DBOS.step()
def pre_filter(event: WebhookEvent, state: SubscriberState) -> PreFilterResult:
    """Deterministically route a webhook event. See the module docstring.

    `state` is accepted for parity with the VT-3.3 caller and future rules; the
    routing rules are driven entirely by the event.
    """
    # Rule c — duplicate delivery (flagged by the VT-3.3a ingress layer).
    # A duplicate is never re-processed, regardless of content.
    if event.dupe_status:
        return RouteToDirectHandler(
            handler_name="dupe_handler", payload={"reason": "duplicate delivery"}
        )

    # --- Twilio status callbacks ---
    if event.message_type == "status_callback":
        state = event.status_callback_state
        if state == "failed":
            # 'failed' keeps the owner error-notification path; template_error_handler ALSO
            # reconciles the customer-send delivery ledger (VT-564) as a fail-soft first step.
            return RouteToDirectHandler(
                handler_name="template_error_handler",
                payload={"twilio_message_sid": event.twilio_message_sid},
            )
        if state in ("delivered", "read", "undelivered"):
            # VT-564 — reconcile the customer-send delivery ledger. 'undelivered' is a delivery
            # FAILURE (stamps the ledger + fires the reviewer outbound_failure alert);
            # 'delivered'/'read' record positive evidence (no alert). A no-op when the sid is not a
            # customer send (owner notifications reconcile in the runner, VT-524).
            return RouteToDirectHandler(
                handler_name="customer_send_delivery_handler",
                payload={"twilio_message_sid": event.twilio_message_sid},
            )
        # missing/unknown state — conservative: let the brain decide.
        return RouteToBrain(reason=f"status callback state '{state}' — needs review")

    # --- Inbound message body checks ---
    normalized = _normalize(event.body)
    nfc_body = _nfc(event.body)  # VT-329: pattern searches run on the NFC form

    # Rule a — opt-out keyword (VT-329: boundary-safe CONTAINMENT, NFC, EN + Devanagari + Hinglish;
    # was whole-body-exact, which missed "please बंद करो" / danda variants). Checked FIRST so a
    # mixed "enable ... STOP" yields to the opt-out (DPDP-safe).
    for pattern in _OPT_OUT_PATTERNS:
        if pattern.search(nfc_body):
            return RouteToDirectHandler(
                handler_name="opt_out_handler", payload={"matched": pattern.pattern}
            )

    # Rule a2 — VT-303 data-inputs ENABLE keyword (exact, case-insensitive, NFC).
    # The consent-grant phrase. Routed here (a direct handler, no LLM) so an
    # owner whose owner_inputs is still FALSE can turn it on — the gate on the
    # brain transmit lives in runner.webhook_pipeline_run.
    if normalized in _ENABLE_KEYWORDS:
        return RouteToDirectHandler(
            handler_name="data_inputs_enable_handler", payload={"matched": normalized}
        )

    # Rule b — DSR keyword (VT-329: boundary-safe containment, NFC, EN + Devanagari + Hinglish).
    for pattern in _DSR_PATTERNS:
        if pattern.search(nfc_body):
            return RouteToDirectHandler(
                handler_name="dsr_handler", payload={"matched": pattern.pattern}
            )

    # Rule b2 — VT-384 L3 autonomy KILL keyword (boundary-safe containment, NFC, EN + Devanagari
    # + Hinglish). Ordered AFTER opt-out + DSR (the CL-438 floor + RULE_ORDER pin): a phrase that
    # is ALSO an opt-out ("STOP") never reaches here — it was already routed to opt_out_handler.
    # An autonomy-specific kill ("stop automatic sending") freezes L3 via the substrate kill path
    # (autonomy_kill_handler -> record_regression_event('owner_keyword'), which cancels in-flight
    # holds/batches same-txn) WITHOUT a full DPDP opt-out.
    for pattern in _L3_KILL_PATTERNS:
        if pattern.search(nfc_body):
            return RouteToDirectHandler(
                handler_name="autonomy_kill_handler", payload={"matched": pattern.pattern}
            )

    # Rule b3 — VT-384 L3 autonomy ENABLE keyword (exact whole-body match, case-insensitive, NFC).
    # The deliberate opt-in verb the team_autonomy_offer promises ("Reply ENABLE"). Ordered AFTER
    # opt-out + DSR (CL-438 floor): an owner who somehow sends both an opt-out and ENABLE yields to
    # the opt-out. Routes to autonomy_enable_handler, which resolves the open autonomy_upgrade
    # approval + grants L3 (grant_l3 re-validates the streak in-txn — a stale grant no-ops).
    if normalized in _L3_ENABLE_KEYWORDS:
        return RouteToDirectHandler(
            handler_name="autonomy_enable_handler", payload={"matched": normalized}
        )

    # Rule f — status ping (narrow whole-message regex).
    if _STATUS_PING.match(event.body):
        return RouteToDirectHandler(handler_name="status_ping_handler")

    # Rule g — integration intent (VT-206 Q4). Precise regex bias toward
    # false-negative: ambiguous phrases fall through to brain.
    if _INTEGRATION_INTENT_RE.search(event.body):
        return RouteToBrain(reason="integration_intent — owner wants to add/connect a data source")

    # Rule h — everything else needs the orchestrator-agent brain (VT-3.9).
    if event.message_type == "unknown":
        return RouteToBrain(reason="unknown message type — needs reasoning")
    return RouteToBrain(reason="substantive owner message — needs orchestrator-agent reasoning")
