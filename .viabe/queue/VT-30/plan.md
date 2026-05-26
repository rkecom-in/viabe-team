---
task: VT-30
author: claudecode
ts: 2026-05-26T17:00:00+05:30
estimated_tokens: 110000
estimated_minutes: 110
---

## TL;DR

Ship the deterministic `compose_owner_output` composer at `src/orchestrator/output_composer.py` + new `config/template_routing.yaml` + tool-registration wrapper at `agent/tools/compose_output.py` + 8 honesty-rule unit tests + 10-assertion canary (zero LLM). 6-canary regression sweep. Single PR ~110K.

## Approach

### 1. `output_composer.py` — pure deterministic Python

`compose_owner_output(specialist_result: AgentResult | None, state: SubscriberState, intent_or_trigger: str) -> ComposedOutput`:

```python
@dataclass(frozen=True)
class ComposedOutput:
    message_body: str
    message_type: Literal["free_form_24h", "template"]
    template_name: str | None       # populated when message_type='template'
    template_params: dict[str, str] # variable substitution for the template
    urgency: Literal["low", "medium", "high", "critical"]
    follow_up_required: bool
    follow_up_intent: str | None
    redaction_safe: bool            # internal flag — set False if any field carries raw PII
    preferred_language: Literal["en", "hi"]
```

Composition rule order (deterministic precedence):
1. **24h-window check** — `state.last_owner_message_at + 24h <= now` → `message_type='template'`. Else free-form path eligible.
2. **Refund acknowledgment** — `state.phase == 'refunded'` → MUST mention refund decision (template or free-form prefix).
3. **Escalation framing** — `state.escalation_pending` → prepend "agent encountered an issue" honest framing.
4. **Hard-limit explanation** — `specialist_result.status == 'terminated'` → identify `terminated_by` (HardLimitAxis: tokens/tools/depth/wallclock/cost_paise); plain-language explanation.
5. **Template selection** — load `template_routing.yaml`; look up `(intent_or_trigger, phase)` → template_name; cross-reference `twilio_templates.yaml` for content_sid existence.
6. **Honesty enforcement** — regex-based + structural checks for:
   - No ARRR overstatement (looks for `attribution_uncertain=True` → "approximately"/"~" prefix on monetary mentions)
   - No retention pressure (regex deny-list: `r"(are you SURE|sure\\?\\s*Look|but you|don't leave)"`)
   - No certainty claims about customer intent (specialist_result with inferred intent → frame as "pattern suggests" / "looks like")
   - No hidden failures (if `terminated_by` set → message MUST mention the agent issue)
7. **Language selection** — `tenant.preferred_language` (env or constant fallback for now; full tenant.preferred_language column wires in VT-9.2). Mixed-language inputs pass through untouched.

### 2. `template_routing.yaml` — NEW

Mapping table: `(intent_or_trigger, phase)` → `template_name`. Names link to `twilio_templates.yaml` SIDs already on main.

```yaml
welcome:
  onboarding:    team_welcome
  trial:         team_welcome
weekly_approval:
  paid_active:   team_weekly_approval
  paid_at_risk:  team_weekly_approval
opt_out_confirmed:
  any:           team_opt_out_confirmation
dsr_acknowledged:
  any:           team_dsr_acknowledgment
agent_stuck:
  any:           team_agent_stuck_escalation
status_ping:
  any:           team_status_ping
unable_to_complete:
  any:           team_unable_to_complete_request
error_handler:
  any:           team_error_handler
```

(8 Tier-A names map 1:1 from `twilio_templates.yaml`.)

### 3. `agent/tools/compose_output.py` — tool registration

Same pattern as `agent/tools/self_evaluate.py`. Adapter that the orchestrator-agent's tool inventory can dispatch. Tool description matches brief spec: "Compose an owner-facing WhatsApp message from a specialist result + current state. Returns structured ComposedOutput."

Tool invocation flows: `orchestrator-agent → compose_owner_output(args) → ComposedOutput → downstream send_template_message (template path) OR returned to agent for free-form-24h reasoning (free_form_24h path; actual free-form send wrapper deferred — see Q1)`.

### 4. Observability wiring

- `traced_node` decorator on `compose_owner_output` for Logfire span (VT-171).
- `log_event(event_type='composer_invoked', ...)` for pipeline_log audit trail. Event type registered in `observability/event_schemas.py`.
- All payload fields flow through `redact_for_log` at the writer boundary (VT-104). Composer surface itself doesn't touch PII directly — just packages.

### 5. Tests

- `tests/orchestrator/test_output_composer.py` — pure unit tests covering all 8 honesty rules + 24h window + Tier-A routing + language selection + mixed-language passthrough + refund acknowledgment + escalation framing + hard-limit explanation.
- `tests/orchestrator/agent/tools/test_compose_output.py` — tool registration shape; orchestrator-agent dispatch shape (no actual agent invocation here; that's VT-125 scope).

### 6. Canary

`canaries/vt30_output_composer.py` — 10 assertions across 3 groups (Group A regression / Group B honesty rules / Group C routing). Zero LLM (PREFLIGHT enforces `ANTHROPIC_API_KEY` ABSENT). NO actual WhatsApp send (would message Fazal); Twilio Content API token used for SID validation only.

## File changes

- **NEW** `apps/team-orchestrator/src/orchestrator/output_composer.py`
- **NEW** `apps/team-orchestrator/src/orchestrator/agent/tools/compose_output.py`
- **NEW** `apps/team-orchestrator/config/template_routing.yaml`
- **MODIFY** `apps/team-orchestrator/src/orchestrator/observability/event_schemas.py` — register `composer_invoked` event type
- **NEW** `apps/team-orchestrator/tests/orchestrator/test_output_composer.py`
- **NEW** `apps/team-orchestrator/tests/orchestrator/agent/tools/test_compose_output.py`
- **NEW** `apps/team-orchestrator/canaries/vt30_output_composer.py`

## Test plan

- `pytest tests/orchestrator/test_output_composer.py tests/orchestrator/agent/tools/test_compose_output.py` — full pass
- `pytest tests/` orchestrator-wide — zero regression
- `ruff check apps/team-orchestrator` — clean
- VT-30 canary 10/10 PASS (real Supabase for SubscriberState fixtures, NO Anthropic, real Twilio Content API token for SID validation only)
- 6-canary regression sweep: VT-102 7/7 + VT-103 8/8 + VT-104 10/10 + VT-171 11/11 + VT-175 8/8 + VT-176 10/10 = 54 assertions byte-identical

## Risks

1. **Free-form `send_whatsapp_message` wrapper does NOT exist** on main. Brief says "may need a thin wrapper — verify at STEP-0." STEP-0 confirms it's absent. **Q1 below — defer free-form send wrapper to downstream row OR write thin wrapper in this PR?**

2. **Tool registration timing vs VT-125.** VT-125 (orchestrator-agent prompt + tool inventory expansion) is Backlog exec 9. Composer-tool registration in this PR uses the same shape as `self_evaluate.py`. VT-125 can pick up the tool from the inventory without re-registering. **No coordination problem; documented in module docstring.**

3. **`tenant.preferred_language` column doesn't exist** (VT-9.2 sign-up). Composer needs a fallback. Plan: default to `"en"` via a constant + env override `TENANT_DEFAULT_LANGUAGE`; when VT-9.2 ships, swap the lookup to `tenants.preferred_language`. Composer surface unchanged. Documented in module docstring.

4. **CodeX-rejection rule from brief — "send_whatsapp_message / send_whatsapp_template reject calls where text didn't pass through compose_owner_output (via hash signature)".** Hash-signature gate is forward-pointing; the existing `send_template_message` doesn't validate signatures. Implementing the gate in this PR adds scope. **Q2 below — defer the signature gate to VT-125 OR ship a stub guard in this PR?**

5. **Honesty rules' regex deny-list completeness.** Phrase-list approach is fragile against creative re-phrasings. Plan: regex covers the brief's explicit examples + 4 additional pressure phrases I'll source from the `concept-team.md` / `concept-team-pillars.md` Pillar 7 sections. Documented + extensible. Future LLM-based heuristic checker can ship as a separate row.

6. **6-canary regression sweep budget.** ~26+50+25+26+32+14 = ~173s sequential. Within `pre-merge-result` audit window, not canary wall-clock.

7. **gate-no-llm-in-deterministic-triggers extension.** Brief asks for confirmation that the gate "would-extend to `output_composer.py` if scope expanded." Composer is deterministic by spec. **Q3 below — extend the gate to `output_composer.py` whole-file in this PR (~3 lines)?**

## Plan-ready questions

### Q1 — free-form `send_whatsapp_message` wrapper

STEP-0 confirms NO free-form wrapper exists on main. Two options:
- **(A) Recommend** — defer wrapper to downstream row (VT-5.x successor). Composer outputs `ComposedOutput` with `message_type='free_form_24h'`; the caller (orchestrator-agent or test fixture) handles the send manually until a wrapper ships. Keeps VT-30 scope tight; canary asserts the composer's `message_type` field is correctly set + does NOT attempt a free-form send.
- **(B)** Write a thin `send_whatsapp_message(to_number, body, run_id) -> dict` wrapper in `utils/twilio_send.py` paralleling `send_template_message`. Adds ~30 LOC + a canary assertion that the wrapper-would-succeed via dry-run.

**Recommend (A).** Free-form sending without templates is the riskier path (24h-window logic + Meta moderation); a dedicated PR with deeper review is warranted. Composer ship is the priority; sending is downstream.

### Q2 — CodeX hash-signature gate (brief §6 "Tool registration for orchestrator-agent")

Brief specifies: `send_whatsapp_message / send_whatsapp_template tools reject calls where text didn't pass through compose_owner_output via a hash signature in the call payload`. This is a runtime enforcement, not a code-level gate.

**Recommend defer (A) to VT-125** — when the orchestrator-agent's tool inventory expands and the agent actually dispatches `send_whatsapp_message`, that's the natural seam to add the signature check. In this PR: composer returns a `signature` field on `ComposedOutput`; consuming tools can pick it up later. Lossless toward the brief intent.

Alternative (B): ship the signature check on `send_template_message` in this PR. Touches an unrelated file + risks breaking existing 5 direct-handler callers.

### Q3 — Extend `gate-no-llm-in-deterministic-triggers` to `output_composer.py`?

Composer is deterministic by spec. The gate currently scans `scheduled_triggers.py` body targets + `billing/*.py` whole-file (VT-175 extension).

**Recommend YES** — add `apps/team-orchestrator/src/orchestrator/output_composer.py` to the whole-file scan list. 1-line extension. Pillar 1 + Pillar 8 enforcement at code level, complementing canary runtime assertions in Group B.

## Status

`.viabe/queue/VT-30/status` flipped `queued` → `planning` → `review`. Signalling plan-ready. Will proceed on APPROVED.
