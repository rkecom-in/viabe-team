# VT-490 — SR cohort-surfacing: REUSE-ASSESSMENT + plan (read-only, plan-first)

**Branch:** `cc-winback-followups` @ `10b3dc3` · **Author:** CC (Claude Code), read-only investigation
**Verdict: PATH A (REUSE an already-approved mechanism).** This is a RE-WIRE of the VT-369
customer-fact-bundle + CL-390/CL-425 envelope into the brain's conversational SR lane — **NOT a new
PII surfacing decision.** Routes to **Cowork's audit-after gate (money + PII)**, NOT a fresh Fazal
privacy-envelope decision.

One correction to the brief's PATH-A framing is load-bearing and is called out below: **k-anonymity
does NOT apply** to a tenant's own customers — it is a cross-tenant control. The real caps are
cohort-size + token-budget + minimum-necessary-fields + CL-425 + CL-390 (all already in the repo).

---

## 1. The blocker, confirmed against code

The brain's conversational SR lane is fed by `build_sales_recovery_context` and runs
`run_sales_recovery_agent`, which has **no tools**, so the cohort must be SUPPLIED in-context:

- `build_sales_recovery_context` — `context_builder.py:595`. Builds only the **aggregate**
  `LedgerSummary` (`context_builder.py:91-101`, populated by `_build_ledger_summary`
  `context_builder.py:298-348`): `total_customers` + `recency_days_pctl` + `spend_paise_pctl` +
  `business_type`. **No `customer_id`, no per-customer row.**
- `serialize_bundle_for_prompt` (`context_builder.py:844`) renders those percentiles as prose
  (`context_builder.py:898-910`) — there is no cohort section to render.
- `run_sales_recovery_agent` — `agent/sales_recovery.py:389`; `tools: dict = {}` at
  `agent/sales_recovery.py:445`. The agent cannot query customers.
- Live wiring: supervisor `_sales_recovery_node` (`supervisor.py:72` → `run_sales_recovery_agent`
  at `supervisor.py:128`), reached via `dispatch_brain` (`runner.py:869`) → `spawn_sales_recovery`
  handoff (`handoffs.py:201`, `_build_sales_recovery_update`).
- The prompt **requires a supplied cohort**: `agent/prompts/sales_recovery_v1.md:45-51` ("Treat
  every request that would require customer data … as `insufficient_data` … except scenarios where
  the orchestrator has already supplied the cohort"). With aggregate-only context it correctly
  returns `insufficient_data` (`sales_recovery_v1.md:145-150`).
- The brain's OUTPUT schema already DEMANDS the rows: `TargetCohort.customer_ids: list[UUID]`
  (`agent/schemas/campaign_plan.py:164-187`, `customer_ids` at `:175`), consumed downstream at
  `collapse.py:138`. **Supplying `customer_ids` to the brain is the schema's intended input, not a
  novel disclosure.**

VT-485 fixed the recency MATH (recency = later of `last_inbound_at` / latest purchase `entry_date`,
`context_builder.py:305-311`). The cohort-SURFACING gap is what remains.

## 2. The existing VT-369 mechanism (the thing to reuse) — file:line evidence

Everything the brief describes already exists in `agents/sales_recovery_executor.py` and is wired
into the **autonomous coordinator-sweep path** (`SalesRecoveryAgent.execute_item`, `:668`) — a
DIFFERENT lane from the conversational brain, but the SAME tenant, SAME data class.

- **Per-customer dormant-cohort SELECTION** (the list IS computed today, just not surfaced to the
  brain): `detect_lapsed_customers(tenant_id, conn, limit)` → `list[LapsedCandidate]`
  (`sales_recovery_executor.py:248`). SQL = `_LAPSED_CANDIDATES_SQL`
  (`db/wrappers/__init__.py:37`, method `lapsed_candidates` `:482`): p75 recency / p50 spend
  thresholds, `opt_out_status='subscribed'`, `complaint_status != 'open'`, active
  marketing-cleared consent (`record_of_consent` ∈ `MARKETING_CONSENT_VERSIONS`), 30-day recontact
  suppression, richest-first, `LIMIT` (`DEFAULT_DETECTION_LIMIT = 50`, `:141`).
- **Redacted per-customer fact bundle:** `build_customer_fact_bundle(...)` → `CustomerFactBundle`
  (`sales_recovery_executor.py:294` / dataclass `:203`). Fields: `customer_id`, `display_name`,
  `days_since_last_sale`, `last_sale_amount_paise`, `lifetime_spend_paise`, `business_name`.
  **NO raw phone, NO email** (docstring `:300-301`; phone-shape rejected in `validate_draft_params`
  `:429`). Every number computed in Python, not by the LLM.
- **Validator-enforced grounding:** `validate_draft_params(params, bundle)`
  (`sales_recovery_executor.py:418`) — keys EXACTLY the template params, every value the exact
  bundle literal, phone-shaped values fail regardless. Ungrounded → dropped, never repaired.
- **CL-425 consent gate (fail-closed):** `_owner_inputs_ok` (`sales_recovery_executor.py:514`) →
  `_owner_inputs_enabled` (`memory/l0_writer.py:31`). Any read error fails CLOSED.
- **CL-390 redaction posture:** module docstring `sales_recovery_executor.py:19-24` — "logs carry
  IDs + counters ONLY — never a display name, phone, or fact bundle"; the bundle is "built, used
  for ONE Messages-API call, and discarded — it never enters workflow state or DBOS step outputs."
- **C2 marketing allowlist (prod fail-closed):** `MARKETING_CONSENT_VERSIONS`
  (`sales_recovery_executor.py:66-135`) — EMPTY default; non-empty only on dev; boot-refusal under
  `VIABE_ENV=production` (`_assert_consent_versions_prod_safe` `:100`).

**Conclusion on the data class:** the executor ALREADY transmits exactly these
`CustomerFactBundle` fields to an Anthropic Messages call behind the CL-425 gate. Surfacing the SAME
fields to the brain's Anthropic call is the **same data class, same vendor egress, same consent
envelope** — no new PII class, no new disclosure surface.

## 3. Why this is PATH A, not PATH B

| PATH-B trigger ("new privacy envelope required") | Reality |
|---|---|
| New PII class surfaced | NO — identical `CustomerFactBundle` field set already approved/in-use (`:203`). |
| New egress surface | NO — same Anthropic Messages call, behind the same CL-425 gate. |
| No existing redaction/consent control | NO — CL-390 (`:19-24`), CL-425 (`:514`), C2 allowlist (`:66-135`), grounding validator (`:418`) all exist. |
| Schema not designed to receive it | NO — `TargetCohort.customer_ids` is the brain's declared output input (`campaign_plan.py:175`). |

All four PATH-B triggers are NEGATIVE → **PATH A**. Governance: reuse of an approved envelope →
**Cowork money+PII audit-after gate**; **no new Fazal privacy decision required.**

### Correction the gate must record: k-anon is N/A here
The brief's PATH-A line says "k-anon + token-budget cap on cohort size." **k-anonymity does not
apply to a tenant's own customers.** In this repo k-anon "counts TENANTS across the whole
workspace" (`privacy/k_anonymity.py:1-4`) — it is the CROSS-TENANT L3-prior admission gate
(`context_builder.py:318` calls the L3 `recency_band` "a SEPARATE plane"). A tenant's own dormant
customers are the owner's lawful first-party data; applying a k≥N suppression to them is a category
error and would wrongly drop small but legitimate cohorts. The correct, already-present caps are
listed in §4.

## 4. The re-wire plan (PATH A)

Reuse the §2 functions verbatim; attach them at the brain's context seam. No new privacy primitive.

**Build steps**
1. **New minimum-necessary cohort section on `SalesRecoveryContext`** (`context_builder.py:209-242`).
   Add `dormant_cohort: DormantCohort` (frozen dataclass, safe-empty default per the CL-190 pattern
   the other sections use). Carry the **minimum-necessary** subset only — start with
   `customer_id` + `display_name` + `days_since_last_sale`; **defer `last_sale_amount_paise` /
   `lifetime_spend_paise` per-customer** unless the brief that lands shows the brain needs them
   (the aggregate spend percentiles already give it the spend picture). Plus a
   `data_completeness["dormant_cohort"]` flag and a `truncated`/`total_available` count.
2. **Populate it in `build_sales_recovery_context`** (`context_builder.py:595`) by calling the
   EXISTING `detect_lapsed_customers` + `build_customer_fact_bundle` (`sales_recovery_executor.py`)
   on a `tenant_connection` — the same RLS path `_build_ledger_summary` already uses. Map each
   bundle → the minimum-necessary section row. (Move/share the helpers if an import cycle appears;
   the detection SQL already lives in the `db/wrappers` layer, so prefer importing the wrapper +
   `CustomerFactBundle` rather than the executor module to avoid the coordinator import surface.)
3. **CL-425 gate BEFORE surfacing** — reuse `_owner_inputs_enabled` (`memory/l0_writer.py:31`),
   exactly as the executor's `_owner_inputs_ok` does. Gate FALSE / read-error → emit the
   safe-empty `DormantCohort` (`available=False`) so the brain falls back to `insufficient_data`
   cleanly. Fail-closed, no PII transmit.
4. **Cohort-size cap** — reuse `DEFAULT_DETECTION_LIMIT = 50` (`sales_recovery_executor.py:141`);
   surface `total_available` + a `truncated` flag so the brain knows the cohort is capped.
5. **Token-budget + truncation order** — extend the existing 8K cap loop
   (`context_builder.py:671-697`). Per VT-71 the moat layers (L3/L4) are protected and per-tenant
   sections trim first. Insert `dormant_cohort` into the truncation order so an over-budget bundle
   sheds cohort rows (newest-spend-last or oldest-recency-first) BEFORE it ever touches L3/L4 —
   trim rows, then drop the section to `available=False` as the last per-tenant step.
6. **Render in `serialize_bundle_for_prompt`** (`context_builder.py:844`) — a `## Dormant cohort
   (candidate customers; YOU pick the final target subset)` block listing `customer_id` +
   `display_name` + `days_since_last_sale`, with the `substrate_populated` + `truncated` markers
   the other sections use.
7. **CL-390 hygiene** — the per-customer rows are PROMPT-ONLY. Do NOT persist them into
   `composition_audits` (`_write_composition_audit` `context_builder.py:558` passes
   `cohort_key=None` today — keep raw rows OUT of `section_token_counts`/audit). Keep logs at
   IDs+counters. The bundle dies with the run (LangGraph state already excludes raw PII at
   `serialize`-time).
8. **Prompt note (no schema change)** — `sales_recovery_v1.md` already documents the
   supplied-cohort path; a one-line pointer that the cohort now arrives under `## Dormant cohort`
   is enough. The output `TargetCohort.customer_ids` schema is unchanged and already validated
   (`campaign_plan.py:180-187` enforces `cohort_size == len(customer_ids)`).

**Acceptance / canary (Rule #15)**
- Unit: gate-FALSE → `available=False` → brain `insufficient_data`; gate-TRUE with seeded lapsed
  customers → cohort rows present → brain returns `proposed` with `target_cohort.customer_ids` ⊆
  the surfaced ids (the brain may not invent an id not in the supplied set — assert subset).
- Token cap: a synthetic 50-row cohort stays under the 8K cap or trims per §4.5 without starving
  L3/L4.
- Redaction: assert no `phone`/`email` shape can reach the section (reuse the executor's phone-shape
  guard at the row-build boundary); assert `composition_audits` carries no raw row.
- **Validate on deployed dev** (CL-2026-06-29), `MARKETING_CONSENT_VERSIONS` set on dev only;
  prod stays empty/boot-fail-closed. NO real-number send — drafting only; the binding SEND gate
  (`customer_send.agent_send_draft` Gate 0) is untouched.

## 5. Open design fork for Cowork's gate (does NOT change the A/B verdict)

Both options reuse the SAME VT-369 mechanism; the only question is WHERE the reuse seam attaches:

- **(a) Wire detection into the brain context** (this plan): the conversational brain drafts the
  `CampaignPlan` itself. Smallest change to the live `dispatch_brain` lane; keeps the
  owner-conversational UX (WhatsApp-first, CL-443) intact.
- **(b) Brain DELEGATES to the autonomous executor** (`SalesRecoveryAgent.execute_item`,
  `sales_recovery_executor.py:668`) rather than re-surfacing the cohort into chat. The executor
  already detects→bundles→drafts→grounds→persists `agent_drafts`→arms Pillar-7. The brain would
  just trigger a sweep/work-item and report status.

(a) is the minimal fix for the win-back re-drive blocker and is what this plan details. (b) avoids
two SR drafting implementations long-term but is a larger routing change. **Recommend (a) for the
re-drive unblock; flag (b) as a follow-up consolidation** — Cowork's call at the gate. Neither
invents new PII surfacing.

## 6. Bottom line

- **PATH A.** Re-wire the existing VT-369 `CustomerFactBundle` + `detect_lapsed_customers` +
  CL-390/CL-425/C2 envelope into `build_sales_recovery_context`. Same data class, same vendor
  egress, same approved consent/redaction controls; the brain's own output schema already demands
  the `customer_ids`.
- **No new privacy envelope; no fresh Fazal privacy decision.** Goes to Cowork's money+PII
  audit-after gate.
- **k-anon explicitly N/A** (cross-tenant control); real caps = cohort-size (50) + 8K token-budget +
  minimum-necessary fields + CL-425 + CL-390.
