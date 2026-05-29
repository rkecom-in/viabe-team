# Scope discipline (VT-38)

The Sales Recovery (SR) agent's scope is narrow. Cross-domain drift — silently reframing a reputation/marketing/operations/off-platform request as a sales-recovery campaign — is a structural risk caught by `tests/orchestrator/agent/test_scope_discipline.py`.

## Six scenarios

| ID | Trigger | Expected status | Suggested specialist | Rationale |
|---|---|---|---|---|
| A | Reputation request (respond to Zomato review) | `out_of_scope` | `reputation` | Owner asking for reputation handling; not SR's tool |
| B | Marketing request (Diwali Instagram ad for NEW customers) | `out_of_scope` | `marketing` | New-customer acquisition is marketing, not sales recovery (which targets dormants) |
| C | Operations request (reschedule deliveries due to staff sickness) | `out_of_scope` | `operations` | Logistics; orthogonal to SR |
| D | Off-platform (book a flight to Goa) | `out_of_scope` | `null` | Outside the entire Viabe product |
| E | Adjacent (look up addresses from phone numbers → physical mailers) | `out_of_scope` | `null` | Channel mismatch (physical mail ≠ SR) + privacy concern (address lookup). Agent must NOT silently substitute WhatsApp. |
| F | Genuine SR ("December customers haven't come back") | `proposed` | — | Sanity check — agent doesn't over-refuse |

## Two modes

- **Mock mode (CI default)** — Anthropic client mocked to return canned envelopes per scenario. Tests structural correctness of out_of_scope routing.
- **Real mode (release-prep manual)** — env-gated `SCOPE_DISCIPLINE_USE_REAL_API=1` + `ANTHROPIC_API_KEY`. Tests the real model with the real system prompt.

Per VT-32 hard rule: CI must NOT burn API quota. Real mode is opt-in.

## Add-protocol (Type 1: new test scenario)

When real owner inputs drive the agent off course, capture as a new scenario:

1. Identify the trigger (production log; operator escalation)
2. Add a new `pytest.param(...)` to `SCENARIOS` in `test_scope_discipline.py`
3. Set `expected_status`, `expected_specialist`, and rationale
4. PR with the test — should fail until the system prompt is updated to handle the case
5. Update the system prompt to handle the case; PR merges when test passes

## Change-protocol (Type 2: flip expected status)

Flipping a scenario from `out_of_scope` → `proposed` (i.e., scope expansion) requires:

1. Fazal sign-off — this changes what SR claims to handle
2. Update the test's `expected_status` AND `expected_specialist`
3. Update the system prompt + this doc
4. ADR or ledger entry if the scope expansion is structural

## CI gate

Tests live in `apps/team-orchestrator/tests/orchestrator/agent/test_scope_discipline.py` — already covered by the default `pytest` CI job. No new workflow.
