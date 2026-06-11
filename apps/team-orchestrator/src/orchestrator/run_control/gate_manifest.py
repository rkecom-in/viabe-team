"""VT-374 run-control gate manifest (plan §4 F14 — canonical deny-list).

The send / consent / approval / compliance surfaces that run-control must NEVER
treat as controllable steps (I2: send + approval gates are structurally
non-overridable; I6: opt-out/DSR/consent processing is pause-EXEMPT by
construction). Two enforcement layers consume this frozenset:

1. ``orchestrator.run_control.registry`` raises ``RuntimeError`` at import time
   if any registered ``controllable`` step maps to a module listed here.
2. The CI grep gate (``tests/orchestrator/test_gate_manifest_grep.py``) fails
   when a send/consent-shaped module exists under src/orchestrator outside this
   manifest — a NEW send surface must be added here before it ships.

Every dotted path below is verified against the real tree (2026-06-12). Two
plan-name corrections: the plan's ``orchestrator.pre_filter`` is actually
``orchestrator.pre_filter_gate``, and "customer_inbound" has no module of its
own — that gate lives in ``pre_filter_gate`` + the direct handlers, all listed.

STDLIB-ONLY by design: the dep-less CI smoke imports this module (no
langgraph / dbos / psycopg may load at import time).
"""

from __future__ import annotations

GATE_MODULES: frozenset[str] = frozenset(
    {
        # Customer/owner-facing WhatsApp send surfaces (I2).
        "orchestrator.agents.customer_send",
        "orchestrator.utils.twilio_send",
        "orchestrator.agent.tools.send_whatsapp_message",
        "orchestrator.agent.tools.send_whatsapp_template",
        "orchestrator.owner_surface.freeform_acks",
        # Owner-approval gates (Pillar 7: approvals are never inherited/overridden).
        "orchestrator.agent.tools.request_owner_approval",
        "orchestrator.agent.approval_resume",
        "orchestrator.agents.approval_glue",
        # Consent / compliance surfaces (I6 pause-exempt fast paths + lawful-basis
        # capture). The opt-out/DSR direct handlers are listed so no future registry
        # entry can ever make a compliance path holdable.
        "orchestrator.privacy.consent",
        "orchestrator.api.consent_capture",
        "orchestrator.direct_handlers.opt_out_handler",
        "orchestrator.direct_handlers.dsr_handler",
        "orchestrator.direct_handlers.consent_required_handler",
        # Inbound classification gate ("customer_inbound" lives here, not in a module
        # of that name) + money-edge state transitions.
        "orchestrator.pre_filter_gate",
        "orchestrator.transitions",
    }
)

__all__ = ["GATE_MODULES"]
