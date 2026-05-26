---
reviewer: cowork
verdict: APPROVED-all-questions
ts: 2026-05-26T16:58:00+05:30
plan_sha: (queue/VT-30/plan.md)
---

# Review — VT-30 plan

**APPROVED on all 3 Qs.** No conditions. Clean STEP-0 ground-truthing (you re-confirmed the corrections in my brief-ready artifacts block). Plan covers honesty rules + 24h window + Tier-A routing + 6-canary regression sweep + tool registration + signature scaffold for the future send-dispatch.

## Q1 (free-form `send_whatsapp_message` wrapper) — APPROVE DEFER

Composer outputs `ComposedOutput.message_type='free_form_24h'`. The actual transport for free-form path ships in a downstream row (likely paired with VT-125 orchestrator-agent tool inventory expansion). Composer's job ends at the structured output; that's the right architectural seam.

## Q2 (CodeX hash-signature gate) — APPROVE DEFER TO VT-125

With 5 direct_handlers as legitimate composer-bypass paths, the original brief's "all sends go through composer" gate would have false-positive'd them. Right call: composer returns `signature` field on `ComposedOutput`; future send-dispatch (VT-125 inventory expansion OR a dedicated wrapper row) verifies signature on agent-path sends only. Direct-handler sends remain unaffected. Architecturally clean.

## Q3 (extend `gate-no-llm-in-deterministic-triggers` to `output_composer.py`) — APPROVE YES

1-line CI gate extension to scan `output_composer.py` whole-file for forbidden LLM tokens. Pattern-match to VT-175's extension to `billing/`. Pillar 1 structural enforcement at code level, complementing Canary Group B (honesty-rule deterministic-by-definition).

## What's confirmed in your plan that I want to acknowledge

- **Risk #3 (preferred_language column absent):** correct — VT-9.2 sign-up not shipped. Fallback constant + env override is the right Phase-1 disposition. Single-tenant test fixtures should explicitly assert the fallback path.
- **Risk #5 (honesty-rule regex completeness):** covering brief examples + 4 additional from Pillar 7 sources is thorough. Fazal-priority on these tests.
- **Risk #6 (6-canary sweep ~173s):** within audit window. Capture wall-clock per-canary in `pre-merge-result`.

## Rule #15 audit standard for `pre-merge-result`

- Canary wall-clock + per-assertion observed values (10 assertions)
- PREFLIGHT confirms `ANTHROPIC_API_KEY` ABSENT (composer is deterministic — defense-in-depth)
- 6-canary regression sweep: VT-102 + VT-103 + VT-104 + VT-171 + VT-28 + VT-176 byte-identical
- Sample `ComposedOutput` JSON for each of the 8 Tier-A intent paths (verbatim)
- 8 honesty-rule unit tests pass + regex assertions inline-cited
- `gate-no-llm-in-deterministic-triggers` extension green on `output_composer.py`
- Full stdout tail ≥ 150 lines + log at `/tmp/vt30-canary-evidence.log`

Summary-only will be bounced.

## Out of scope (concurred)

- Free-form `send_whatsapp_message` wrapper (Q1; downstream row)
- Send-dispatch signature verification (Q2; VT-125 territory)
- Meta template wording / approval (VT-13.3 / Fazal-side)
- `tenant.preferred_language` column impl (VT-9.2)
- Owner-portal display of composed messages (VT-9.7)
- Image / voice attachments outbound (Phase 1 text-only)
- A/B testing message variants (Phase 1.5+)

## Pillar 7

Merge requires Fazal `type: task` with `authorized_by: fazal`. **Fazal personally reviews honesty-rule tests at pre-merge time** — that's the Pillar-7 owner-truth gate. Bounce if honesty-rule tests are weak or skipped.

## Authority

Flip `.viabe/queue/VT-30/status` from `review` → `implementing` and proceed.

Go.
