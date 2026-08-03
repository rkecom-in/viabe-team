"""VT-720 (S4) — the ROUTE CLASSIFICATION contract: gates CLASSIFY, only the composer SPEAKS.

The single-voice program's last structural gap. S1 gave the Manager wire-truth, S2 put every owner
emission through one choke, S3 gave it a ledger of what it has already asserted — and yet four
failure classes survived three independent full-pack x3 runs, all with the same shape:

    a deterministic gate that OWNS A REPLY TEMPLATE speaks without the Manager's context.

Measured (vt721_x3, 3/3 deterministic — not variance):
  - the enforce status line renders a pending city CONFIRM ("we found Surat — is that right?") as the
    ASK label "which city you're based in", inventing ignorance of a fact the draft holds;
  - an inline correction is answered with a canned "is that right" re-confirm;
  - a routing ack claims a draft is "drafted" that the DB disproves.

None of those are prompt problems. Each is a template that asserts state it never checked, in a voice
that is not the Manager's. So S4 does not add a composer — the dispatch/turn brain already IS the
composer, already carrying wire-truth, commitments, the week plan and the asserted-facts ledger. S4
changes WHO IS ALLOWED TO EMIT:

    a converted route returns a RouteClassification -- NEVER reply_text.

What a route KEEPS: deterministic floors retain full VETO power. A hard stop (opt-out / DSR / consent
exactness / a money gate) still acts directly and may TERMINATE a turn without the brain. What it
LOSES is the right to SAY things: its owner-visible line is composed by the brain from the
classification's facts, through the S2 emission choke, against the S3 ledger. Legally-fixed acks
(STOP confirmations, DSR receipts) are explicitly EXEMPT and stay verbatim — see the design note
`.viabe/sprint/vt720-route-unification-design.md` §2.

Fail-soft is structural: every converted route keeps an HONEST deterministic fallback line for the
turn the composer cannot take (LLM error, timeout, no key). "Honest" is load-bearing — the fallback
must not reintroduce the very claim the conversion removed, so converting a route means FIXING its
template, not merely demoting it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The dispositions a classifier may SUGGEST. Advisory to the composer — never an authorization
# (ARCHITECTURE §0.1.1: a suggested disposition is a PLAN, and no plan authorizes an effect).
SUGGESTED_DISPOSITIONS = frozenset({
    "answer_status",       # the owner asked where things stand — answer from the facts below
    "acknowledge_and_continue",  # a fact landed / was corrected — acknowledge, then carry on
    "acknowledge_and_stop",      # a floor matched: acknowledge and stop asking
    "ask_next",            # the honest next thing to ask (facts_missing[0])
    "answer_question",     # the owner asked something the facts below answer
})


@dataclass(frozen=True)
class RouteClassification:
    """What a converted deterministic route hands the composer INSTEAD of a reply.

    ``intent`` — what the route determined the owner's turn IS (route-specific, short snake_case).
    ``facts_known`` — facts the system HOLDS, with values. The composer may state these.
    ``facts_pending_confirm`` — facts held but NOT yet owner-confirmed, ``{field: value}``. The
        composer must treat these as KNOWN-BUT-UNCONFIRMED: it may ask the owner to confirm the
        VALUE, and must never re-ask the field as if it were unknown. This distinction is the whole
        fix for the measured "which city" class.
    ``facts_missing`` — fields genuinely not held, in ask order.
    ``constraints`` — hard statements the composer MUST honor (e.g. "the owner declined: do not ask
        again this turn"). Deterministic findings, not suggestions.
    ``urgency`` — "normal" | "high"; advisory pacing only.
    ``suggested_disposition`` — one of SUGGESTED_DISPOSITIONS; advisory.
    """

    intent: str
    facts_known: dict[str, Any] = field(default_factory=dict)
    facts_pending_confirm: dict[str, Any] = field(default_factory=dict)
    facts_missing: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    urgency: str = "normal"
    suggested_disposition: str = "answer_status"


def _fmt_value(value: Any) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text[:120] if text else "(no value)"


def render_classification_block(c: RouteClassification) -> str:
    """Render the classification as the per-turn ``## Turn classification`` system block.

    These are DETERMINISTIC FINDINGS the composer must honor — not context it may weigh and discard.
    The wording is deliberately imperative about the two things the measured failures got wrong:
    never re-ask a held fact, and never assert state that is not listed here.
    """
    lines: list[str] = [
        "## Turn classification (deterministic — these findings are TRUE; honor them)",
        f"intent: {c.intent}",
    ]
    if c.facts_known:
        lines.append("facts ALREADY KNOWN (never ask for these again — you may state them):")
        lines += [f"  - {k}: {_fmt_value(v)}" for k, v in c.facts_known.items()]
    if c.facts_pending_confirm:
        lines.append(
            "facts KNOWN BUT UNCONFIRMED (you HAVE the value — you may ask the owner to confirm "
            "THIS VALUE; you must NEVER ask for the field as though you did not know it):"
        )
        lines += [f"  - {k}: {_fmt_value(v)}" for k, v in c.facts_pending_confirm.items()]
    if c.facts_missing:
        lines.append(
            "facts GENUINELY MISSING (only these may be asked for): "
            + ", ".join(c.facts_missing)
        )
    if not c.facts_missing and not c.facts_pending_confirm:
        lines.append("facts GENUINELY MISSING: none — nothing is outstanding.")
    if c.constraints:
        lines.append("constraints (binding):")
        lines += [f"  - {s}" for s in c.constraints]
    if c.urgency and c.urgency != "normal":
        lines.append(f"urgency: {c.urgency}")
    lines.append(f"suggested disposition (advisory): {c.suggested_disposition}")
    lines.append(
        "Do NOT claim any state that is not listed above — no 'done', no 'drafted', no 'sent' "
        "unless it appears in facts ALREADY KNOWN."
    )
    return "\n".join(lines)


__all__ = ["SUGGESTED_DISPOSITIONS", "RouteClassification", "render_classification_block"]
