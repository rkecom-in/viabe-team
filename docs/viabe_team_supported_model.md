# Viabe Team — Supported LLM Models, Env Vars & Cost Controls

_Last updated 2026-07-13. Ground truth: `apps/team-orchestrator/src/orchestrator/llm/provider.py` + migrations 173–176._

The orchestrator runs on a **multi-provider LLM seam**: **12 models across 5 providers**, every
model selectable per role via Railway env vars, every call cost-audited, capped by VTR admins, and
(optionally) web/X-search-enabled. No code change is needed to swap a model or provider — it is an
env-var flip + a service restart (env is read at process start).

---

## 1. Model selection — one var per role/tier

Each var picks the concrete model for one role. **Unset or empty → the default.** Read fresh per
call (a mid-process change takes effect on the next restart).

| Var                     | Controls                                        | Default            |
| ----------------------- | ----------------------------------------------- | ------------------ |
| `TEAM_MODEL_ROUTINE`    | brain, simple turns (acks, FAQs, status)        | `claude-haiku-4-5` |
| `TEAM_MODEL_COMPLEX`    | brain, business-reasoning turns **+ triage**    | `claude-sonnet-5`  |
| `TEAM_MODEL_CLASSIFIER` | intent classifier                               | `claude-haiku-4-5` |
| `TEAM_MODEL_SPECIALIST` | specialists + advisory lanes                    | `claude-sonnet-5`  |
| `TEAM_MODEL_REVIEW`     | plan-validation checkpoint                      | `claude-opus-4-8`  |

**v1 provider caveat.** `COMPLEX`, `CLASSIFIER`, and `REVIEW` also feed **Anthropic-only** internal
sites (triage / classify / plan-validation use the raw Anthropic SDK, not the seam). Point those
three at a `claude-*` id **only** — a non-Anthropic id there **fails loud** at call time by design
(a clear error beats a silent wrong-provider call). `TEAM_MODEL_ROUTINE` and `TEAM_MODEL_SPECIALIST`
accept any of the 12.

An unknown/misspelled id fails loud everywhere, naming the full supported set.

---

## 2. Valid model IDs (12) by provider

| Provider          | Model IDs                                                          |
| ----------------- | ----------------------------------------------------------------- |
| Anthropic         | `claude-haiku-4-5`, `claude-sonnet-5`, `claude-opus-4-8`          |
| OpenAI            | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`                    |
| Google            | `gemini-3.5-flash`, `gemini-3.1-flash-lite`, `gemini-3.1-pro-preview` |
| Z.ai / self-host  | `glm-5.2`                                                          |
| xAI               | `grok-4.5`, `grok-4.3`                                             |

Provider is inferred from the id prefix (`claude-*`→anthropic, `gpt-*`→openai, `gemini-*`→google,
`glm-*`→zai, `grok-*`→xai).

---

## 3. Provider credentials & endpoints

| Var                | Purpose                                                        | Status                     |
| ------------------ | ------------------------------------------------------------- | -------------------------- |
| `ANTHROPIC_API_KEY`| any `claude-*` call                                           | working (app runs on it)   |
| `OPENAI_API_KEY`   | any `gpt-5.6-*` call                                          | canary-proven on dev       |
| `GEMINI_API_KEY`   | any `gemini-*` call (explicit pass; renamed from GOOGLE_API_KEY, Fazal 2026-07-19) | needed for the Gemini canary |
| `GLM_API_KEY`      | `glm-5.2` call                                               | needed for the GLM canary  |
| `XAI_API_KEY`      | any `grok-*` call + web/X-search                             | needed for the Grok canary |
| `GLM_BASE_URL`     | GLM endpoint. **Single self-host switch** — point at a self-hosted vLLM/sglang OpenAI-compatible endpoint, zero code change. | default `https://api.z.ai/api/paas/v4/` |
| `XAI_BASE_URL`     | xAI endpoint / proxy switch                                   | default `https://api.x.ai/v1` |

> **Verify keys by USE, not presence.** Railway seals secret values, so `railway variables` reports
> even a live key as "unset." Confirm a provider works by driving one real call and checking for an
> `llm_call_events` row — never by a presence check.

---

## 4. Cost-saving tiers (Flex / Batch)

Both discounts are **50% off input AND output** (verified 2026-07-13 on both providers).

| Var                        | Values                                                                 | Default    |
| -------------------------- | ---------------------------------------------------------------------- | ---------- |
| `TEAM_OPENAI_SERVICE_TIER` | `standard` \| `flex` (50% off, slower, auto-fallback to `auto` on 429) \| `auto` | `standard` |

- **Flex** is **OpenAI-scoped by name** — it never affects Anthropic/Google/GLM/xAI calls (they
  always record `standard` in v1). Proven live: `gpt-5.6-luna` flex booked at exactly half the
  standard token cost.
- **Batch (50%)** applies to the asynchronous measurement/report pipeline (not live turns) — Anthropic
  Batches API + OpenAI Batch. Gemini batch = 50%; **Grok batch = 20%** (grok-4.3 only); GLM has no
  batch tier. Encoded in the `discount_multiplier` per model row.
- Cache reads are billed at 0.1× input on Anthropic/OpenAI/Google (GLM 0.186×, xAI 1.0× = no cache
  discount) — encoded per model row.

---

## 5. Web-search & X-search

Server-side search is available across providers as an **opt-in capability**, off by default.

| Var                      | Effect                                                                          | Default |
| ------------------------ | ------------------------------------------------------------------------------- | ------- |
| `TEAM_ENABLE_WEB_SEARCH` | Master kill switch. `1`/`true` = the capability is live; off = no search at all | **OFF** |

- **Providers:** Anthropic `web_search_20260209`, OpenAI Responses `web_search`, Gemini
  `google_search`, xAI Grok `web_search` **+ `x_search`**. **X-search (X/Twitter posts) is
  xAI-only.** GLM exposes no server web-search → skipped.
- **Scope (deliberate).** Enabled **only on the advisory lanes that answer with public/latest info**:
  **marketing** (web + X), **finance/tax** (web), **tech** (web), **cost-opt** (web). It is **NOT**
  on the gate path (triage / approval / money / consent / onboarding-grounding) or the grounded lanes
  (sales-recovery / accounting) — those stay grounded in tenant DB facts (no-drift contract).
- **To activate:** set `TEAM_ENABLE_WEB_SEARCH=1` **and** point a lane's tier
  (`TEAM_MODEL_SPECIALIST`) at a search-capable model. Both conditions required.
- **Cost:** every search is recorded per-call (`llm_call_events.search_count` / `search_cost_usd`),
  priced from the `search_tool_pricing` table.

---

## 6. Cost audit (always on)

Every LLM call is recorded to **`llm_call_events`** — no env var needed:
`tenant_id` (nullable for platform/tenantless calls), `agent`, `call_site`, `provider`, `model`,
`service_tier`, `tokens_in/out`, cached tokens, **`cost_usd` computed at write** from
`model_pricing`, plus `search_count` / `search_cost_usd`, `request_id`, `occurred_at`.

Prices live in DB tables (`model_pricing`, `search_tool_pricing`), **VTR-tunable** via the ops
console — costing never hard-codes a number.

> **Judge models by ledger `cost_usd` per turn, not the sticker rate.** A cheaper per-token model can
> cost *more* per turn if it burns more tokens (reasoning + larger context). The ledger settles it in
> one query.

---

## 7. Usage caps (VTR-admin only)

| Var                       | Effect                                                                                        | Default |
| ------------------------- | --------------------------------------------------------------------------------------------- | ------- |
| `TEAM_LLM_BUDGET_ENFORCE` | `1`/`true` = hard caps actually **block** (degrade to deterministic paths + honest owner message); off = record-only | **OFF** |

- **Per-tenant** caps (`tenant_llm_limits`) + a **global/platform** cap (`global_llm_limits`):
  monthly cost/token ceilings, `soft_pct` warning threshold.
- **Only VTR admins set limits** — the runtime can read-to-enforce but never self-edit (FORCE RLS +
  SELECT-only policy). Writes go through the VTR ops-console endpoints:
  `vtr-llm-limits` (per-tenant), `vtr-llm-limits-global`, `vtr-llm-usage` (usage + top-10 by cost).
- Soft/hard crossings emit one `tm_audit` notification per period. Enforcement **never bends the
  money/consent gates** — it degrades to deterministic nets with an honest budget message.

---

## 8. Pricing reference (USD per million tokens, verified 2026-07-13)

| Model                    | Input  | Output | Flex/Batch (50%)* | Cache read |
| ------------------------ | ------ | ------ | ----------------- | ---------- |
| claude-haiku-4-5         | $1.00  | $5.00  | $0.50 / $2.50     | 0.1×       |
| claude-sonnet-5          | $2.00† | $10.00†| $1.00 / $5.00     | 0.1×       |
| claude-opus-4-8          | $5.00  | $25.00 | $2.50 / $12.50    | 0.1×       |
| gpt-5.6-sol              | $5.00  | $30.00 | $2.50 / $15.00    | 0.1×       |
| gpt-5.6-terra            | $2.50  | $15.00 | $1.25 / $7.50     | 0.1×       |
| gpt-5.6-luna             | $1.00  | $6.00  | $0.50 / $3.00     | 0.1×       |
| gemini-3.5-flash         | $1.50  | $9.00  | $0.75 / $4.50     | 0.1×       |
| gemini-3.1-flash-lite    | $0.25  | $1.50  | $0.125 / $0.75    | 0.1×       |
| gemini-3.1-pro-preview   | $2.00‡ | $12.00‡| $1.00 / $6.00     | 0.1×       |
| glm-5.2                  | $1.40  | $4.40  | — (no batch tier) | 0.186×     |
| grok-4.5                 | $2.00  | $6.00  | — (no batch tier) | 1.0× (none)|
| grok-4.3                 | $1.25  | $2.50  | $1.00 / $2.00 (20% batch) | 1.0× (none)|

\* Flex is OpenAI-only; the "batch" column is the async-pipeline rate for the others.
† `claude-sonnet-5` is **introductory $2/$10 through 2026-08-31**, then **$3/$15 from 2026-09-01** —
update the `model_pricing` row via the VTR console on Sep 1.
‡ `gemini-3.1-pro-preview` recorded at the ≤200k-context rate; >200k tiers higher ($4/$18). Preview
model — Google may change/retire it.

### Search-tool pricing (USD per 1,000 invocations)

| Provider  | Tool         | Price   | Status              |
| --------- | ------------ | ------- | ------------------- |
| anthropic | web_search   | $10.00  | verified            |
| xai       | web_search   | $5.00   | verified            |
| xai       | x_search     | $5.00   | verified            |
| openai    | web_search   | $10.00  | placeholder (VTR-tunable) |
| google    | web_search   | $35.00  | placeholder (VTR-tunable) |

---

## 9. Quick recipes

- **Cheapest routine turns:** `TEAM_MODEL_ROUTINE=gpt-5.6-luna` + `TEAM_OPENAI_SERVICE_TIER=flex`
  (or `TEAM_MODEL_ROUTINE=gemini-3.1-flash-lite` — cheapest sticker rate; compare by ledger cost).
- **Self-host GLM:** set `GLM_BASE_URL` to your vLLM/sglang endpoint + `GLM_API_KEY`; then VTR
  re-prices the `glm-5.2` row to your amortized infra cost (or zero).
- **Turn on advisory web search:** `TEAM_ENABLE_WEB_SEARCH=1` + `TEAM_MODEL_SPECIALIST=grok-4.5`
  (Grok also gives X-search on the marketing lane).
- **Arm cost caps:** VTR sets `tenant_llm_limits` / `global_llm_limits`, then `TEAM_LLM_BUDGET_ENFORCE=1`.

> **Prod env-var changes require Fazal authorization** (CL-431). Dev is CC-managed. Never echo a
> secret value; set it in the Railway console and reference by name.
