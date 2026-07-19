> **ARCHIVED 2026-07-17 — zero live authority; see docs/README.md.**

# Rail-Harness Findings (VT-460 map, 2026-06-28) — the rail LARGELY EXISTS; VT-460 = verify + close 4 gaps

## KEY FINDING (validates "review existing first")
The LLM brain is ALREADY structurally barred from side-effects:
- Brain tool surface = [escalate_to_fazal, compose_owner_output_tool, write_l0_fragment, query_l0] + handoffs.
- `agent/tool_guardrail.assert_agent_tools_safe` (VT-268) RAISES at graph-build if any agent-callable tool name matches a send/sheet-write/ledger-write substring (FORBIDDEN_CAPABILITY_SUBSTRINGS). Pinned by `tests/agent/test_no_write_tool_surface.py`.
- The sole agent sender = `agents/customer_send.agent_send_draft` with a 7-gate fail-closed stack (Gate-0 onboarded/activation, Gate-1 batch-state CAS, Gate-2 template registry, Gate-3 opt-out/complaint re-read, Gate-4 version-aware marketing consent [EMPTY allowlist ⇒ structurally zero sends today], Gate-5 caps/suppression) → then VT-45 tool re-runs opt-in/opt-out/complaint/cap + idempotency.
⇒ **VT-460 ≠ build-from-scratch.** The brain-rail exists. VT-460 = VERIFY it (adversarial non-bypassability against the EXISTING guard) + CLOSE the 4 gaps below.

## THE 4 GAPS (the real VT-460 work — multi-path asymmetry + transport choke)
1. **Gate-coverage asymmetry.** Only `agent_send_draft` runs the FULL stack. The CAMPAIGN path (`campaign/execute.execute_approved_campaign`) and the CUSTOMER-INBOUND path (`integrations/customer_inbound.handle_customer_inbound`) reach real customer sends WITHOUT Gate-0 (onboarded/activation) + agent caps. "The guarded tool" is really THREE gate profiles. → unify: every customer-send path passes a shared onboarded+consent+caps choke.
2. **WABA go-live gate not universal.** `wa_send_allowed` (WABA must be Meta-verified 'live') is pre-checked ONLY in customer_inbound; the agent + campaign paths discover a not-live WABA only as a downstream Twilio 4xx. → universal fail-closed pre-gate.
3. **No transport-level structural choke.** `utils/twilio_send.send_template_message/send_freeform_message` dispatch to ANY phone with valid creds; only TEAM_TWILIO_MOCK_MODE stops them (a test no-op, not a gate). A future direct caller passing a customer phone bypasses every gate — the boundary is convention + review + the lint, not a structural choke at the transport. → interpose a single guarded customer-send choke at/just-above the transport that EVERY customer send must route through (owner sends exempt + separately classed).
4. **Customer-inbound consent surface thinner.** First-contact intro + opt-in/opt-out acks send to NON-consented customers (intended opt-in solicitation), gated by an intro-once guard (intro_sent_at) not the consent ledger. → keep as an explicit, separately-audited session-send class; don't silently fold under the marketing gate.

## VT-460 reframed scope
(a) Adversarial-verify the EXISTING brain-rail (VT-268 guard + agent_send_draft stack) is non-bypassable — the test matrix set D against the real code. (b) Close gaps 1-3 with a unified deterministic customer-send choke (shared by agent+campaign+inbound paths, owner-sends exempt) + universal WABA pre-gate + a transport-level structural choke. (c) Class the inbound session-send (gap 4) explicitly. REUSE the existing gates (don't re-implement); unify their enforcement point. NOTE: 2 map lenses (gates, brain-surface) failed the structured-output cap — the intelligence-inventory workflow (w1ygfkh4r) + a direct re-read fill them before building.
