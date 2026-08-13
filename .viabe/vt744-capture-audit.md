# VT-744 STEP 1 — CAPTURE AUDIT (read-only)

**Date:** 2026-08-13 · **Lane:** F5 · **Scope:** audit only, no schema, no code changed.
**Rule applied:** every verdict below cites a migration file or a `file:line` that was actually read.
No claim is carried from a document. Where a document and the code disagree, the code wins and the
disagreement is stated.

> **BASELINE — read this before trusting a line number.** This audit was taken against the working
> tree while **other lanes were actively editing four of the files it cites** (`git status`:
> `dsr_purge.py`, `agents/send_frequency.py`, `integrations/hook_links.py`,
> `agent/approval_resume.py`). Line numbers in `dsr_purge.py` already moved by +10 during this
> session. **Table names, column names and behaviours are stable claims; exact line numbers are
> point-in-time.**
>
> **One verdict below is being actively invalidated as it is written, and that is good news:** an
> uncommitted VT-741 lane is adding **`customer_hook_links` (migration 201)** — the
> customer-attributed click table — and has *already registered it in `dsr_purge._PURGE_ORDER` in
> the same change* (visible in `git diff dsr_purge.py`; `migrations/.next-migration` now reads
> `202`). `send_frequency.py` is simultaneously gaining ~300 lines (the ratified tier rule). So
> **§1 field 3 "clicked: NOT CAPTURED" is true of committed `dev` and false within the hour.**
> Re-check that one cell against `git log` before acting on it — VT-744 must not build a click
> table.

---

## THE HEADLINE — say it plainly

**Most of it already exists.** The system has a real, complete-by-construction audit spine
(`tm_audit_log`, mig 147) with ~70 distinct emission sites, a trainable owner-verdict store
(`agent_corrections` + VT-561 snapshot columns), a per-customer contact ledger with delivery *and*
read state (`agent_customer_contacts`, mig 127/161/200), a lifetime owner conversation
(`conversation_log`, mig 164), a supersede-not-edit plan spine (`manager_tasks` / `manager_task_steps`
+ mig 165), and a live revenue-attribution writer (`billing/attribution_writer.py:74`, reachable from
the daily DBOS job at `scheduled_triggers.py:229`).

**Of VT-744's six required fields, four are substantially CAPTURED, two are PARTIAL, none is a
greenfield build.** This row is small. That is the correct outcome.

**But there are four gaps that are genuinely worth closing**, and one of them (§5.1) is the single
highest-value row in the whole ledger by the row's own argument. There is also a **direct conflict
between VT-744's binding constraints and the shipped `tm_audit` contract** (§6) that must be resolved
before any schema is written, and **five pre-existing DSR holes** (§7.1–7.2) that would make exit gate
(f) fail today on tables VT-744 does not even own — one of them (`customers`, holding raw `phone_e164`
and `email`) is a live DPDP defect, not a VT-744 concern.

---

## 1. GAP TABLE — the six required fields

| # | Field (VT-744 §Step 2) | Verdict | Evidence |
|---|---|---|---|
| **1** | **The ACTION** — what was done, when | **CAPTURED** | `tm_audit_log` `event_layer='does'` (mig `147_vt514_tm_audit_log.sql:44-52`). ~70 emission kinds enumerated at `grep event_kind=` across `src/orchestrator` — incl. `send_result`, `approval_resolved`, `business_action`, `plan_created`, `campaign_execution_result`, `customer_list_exported`. Effect ledgers underneath: `campaign_messages` + `send_idempotency_keys` (mig 049), `agent_customer_contacts` (mig 127), `owner_notifications` (mig 150). |
| **1b** | **…on whose AUTHORITY** (autonomous / owner-approved / VTR-directed) | **PARTIAL** | Only the agent-draft send path stamps it: `agent_customer_contacts.autonomy_level` `CHECK IN ('L2','L3')` (`127_vt369_agent_customer_contacts.sql:16`), written at `agents/customer_send.py:764` and `:896`. **L2 ≈ owner-approved, L3 ≈ autonomous — there is no third value, so "VTR-directed" is inexpressible.** The **campaign/template send path carries no authority column at all**: `campaign_messages` (mig `049:66-84`) has none, and `_write_campaign_message` (`agent/tools/send_whatsapp_template.py:489-506`) writes none. Authority on that path is only *inferable* by joining a `pending_approvals` row that may not exist. |
| **2** | **CONTEXT at decision time** | **CAPTURED** | `tm_audit_log.snapshot_id` = sha256 of the assembled context blocks, computed at `agent/dispatch.py:987-1004` and emitted as `event_kind='context_assembled'` with the raw blocks in `input` (`agent/dispatch.py:1005-1018`). Plan step + objective: `manager_tasks.objective` / `manager_task_steps` (mig 151/152), revision-versioned by mig 165. Capability invoked: `tool_invoked` / `tool_result` (`observability/langchain_callback.py:218,230`). Model/cost per call: `llm_call_events` (mig `173:65-79`). Reasoning depth by reference: `reasoning_ref → pipeline_steps` (mig `147:50`). **Honest limit, already documented at `observability/tm_audit.py:120-125`: only emitted reasoning is captured; `snapshot_id` gives input-replayability, not decision-determinism.** |
| **3** | **OUTCOME** — delivered / read / replied / clicked / opted out / blocked / failed | **PARTIAL** | **delivered / read / failed / undelivered: CAPTURED, agent path only** — `agent_customer_contacts.delivery_status` (mig `161`, widened to include `'read'` by mig `200`), reconciled at `agents/customer_send.py:998-1064`. **The reconciler resolves `message_sid` against `agent_customer_contacts` ONLY** (`customer_send.py:1031`), so **a campaign/template send has no delivery state anywhere** — `campaign_messages.send_status` (mig `049:76-78`) records transport acceptance, not delivery. **replied: PARTIAL** — the fact of an inbound is a mutable timestamp (`wa_conversations.last_inbound_at`, mig `070:17`, upserted at `integrations/customer_inbound.py:98-101`; also `customers.last_inbound_at`), **not an append-only event and not attributed to a specific outbound message**. **clicked: NOT CAPTURED** — `hook_links.click_count` (mig `071:20`) is a per-token counter with no customer and no message; VT-741 §THE SIGNALS states the customer-message click table is unbuilt, and no migration up to 200 adds one. **opted out: CAPTURED** — `customers.opt_out_status` (mig `045:32`) + `record_of_consent` (mig 067). **blocked: NOT CAPTURED as a distinct state** — VT-741 §6 requires it; `_DELIVERY_FAILURE_STATES` (`customer_send.py:977`) is `{failed, undelivered}` with no block class. |
| **3b** | **BUSINESS RESULT** (order placed, amount, none) | **CAPTURED — and this surprised me** | `attributions` (mig `023:15-24`) had no writer for a long time and several code comments still say so (`agent/tools/match_transactions.py:30`). **That is stale.** `billing/attribution_writer.py:74` inserts real rows, called from `billing/attribution_close.py:28`, driven by the daily 2 AM IST DBOS handler `attribution_close_scheduled` → `run_attribution_close_body` → `close_attribution` (`scheduled_triggers.py:222-229`). It joins `campaign_recipients` → `customer_ledger_entries` (`entry_type='payment'`, mig 061, written at `integrations/ledger.py:100`) in a 7-day window. **Conditional on ledger ingestion actually producing payment rows for that tenant — but the mechanism is live, not a shell.** |
| **4** | **OWNER'S REACTION** — approved / edited / rejected / ignored | **PARTIAL — and this is the important one** | `agent_corrections` (mig 154) + VT-561 columns (mig 160) is a genuinely good store: `correction_kind IN ('edit','reject','approve')`, `correction_text` PII-redacted **not** sha256'd, `proposal_snapshot` captured *before* `redact_batch_close` destroys the drafts. Written at four sites in `agents/approval_glue.py:322, 364, 402, 434`. **Three concrete holes:** (a) **it only fires for `approval_type='agent_customer_send'`** — `apply_agent_decision` returns `None` for every other type at `agents/approval_glue.py:283-284`, so a **`campaign_send` approval (mig `052:47-49`) — the dominant path today — writes no correction row at all**; (b) **`corrected_snapshot` is a parameter that no caller ever passes** (declared `correction_store.py:138`, inserted `:166`, and `grep corrected_snapshot=` across `src/` returns zero call sites) — so an edit has a *before* and no *after*, which is exactly what exit gate (c) asks for; (c) **"ignored" is not recorded** — the `timeout`/`defer` branch at `approval_glue.py:456-482` writes `redact_batch_close` and a regression counter but **never calls `record_correction`**. |
| **5** | **VTR INTERVENTION and its REASON** | **PARTIAL** | The intervention IS captured: `ops_audit` (mig 074, append-only by convention) written from `escalations.py:119`, `api/ops_common.py:84`, `api/ops_runcontrol.py:87`; plus `tm_audit` `autonomy_change` (`agents/autonomy.py:219,269,328,528,555`) and `ownership_decision` (`api/ops_vtr_console.py:571`). **The REASON is optional at every layer.** `VtrAutonomyOverrideBody.reason: str = ""` (`api/ops_vtr_console.py:102`), `VtrBatchCancelBody.reason: str = ""` (`:108`), and `ops_audit.detail TEXT NULL` (mig `074:20`). **Exit gate (d) — "an override with no reason is rejected at write time" — is not met today, at either the API boundary or the DB.** Separately, `agent_corrections.authority` defaults to `'owner'` and **no call site ever passes `'vtr'`** (`correction_store.py:139`; the four `approval_glue` calls omit it), so a VTR correction is indistinguishable from an owner one in the trainable store. |
| **6** | **What was NOT done** (rejected alternative) | **PARTIAL, leaning NOT CAPTURED** | What exists: **superseded plan steps survive** — `manager_task_steps.status='superseded'` with `plan_revision`, supersede-not-edit by design (mig `165` header; `manager/plan_store.py:470-510`), so an abandoned step keeps its `detail`. `agent_drafts.skip_reason` records *why* a draft was not sent (`agents/customer_send.py:356`, `:92`), and `manager_asserted_facts.status IN ('active','superseded','retracted')` + `superseded_by` (mig `187:33-36`) retains retracted assertions. Non-admitted decisions get a breadcrumb: `campaign_first_contact_not_admitted`, `campaign_revision_not_admitted` (`manager/triage_seam.py:610, 419`), `policy_shadow` (`agents/customer_send_choke.py:235`). **What does NOT exist: the alternative the Manager considered and rejected.** Nothing writes it — `grep -iE "alternative\|counterfactual\|considered_options"` over `src/` returns only `first_data_step/method_selector.py:141` (unrelated, ingestion method choice) and `knowledge/admission.py:446` (offline ablation). And the one place a rejected output *is* recorded — `emission_gate._emit_blocked_audit` — stores **only `sha256(blocked_text)`** (`agent/emission_gate.py:1039-1045`), so the substance of what the Manager was stopped from saying is destroyed by design. |

---

## 2. PER-TABLE INVENTORY — the named list, verified

| Table / module | Exists? | What it actually holds | Verdict for VT-744 |
|---|---|---|---|
| `tm_audit_log` (mig 147) | Yes | The spine. `event_layer` knows/gets/decides/does/asks + `event_kind`, `actor`, `input`/`decision`/`action`/`result` JSONB (all PII-redacted at emit), `trace_id`, `snapshot_id`, `reasoning_ref`, `parent_audit_id`. RLS + FORCE; app_role INSERT-only, operator-JWT SELECT (mig `147:61-84`) ⇒ **append-only for app_role**. In `_PURGE_ORDER` (`dsr_purge.py:256`). | **CAPTURED** — this is the ledger VT-744 asks for, already built. |
| `agent_corrections` (mig 154) + pairs (mig 160) | Yes | `correction_kind`/`decision_verb`/`correction_text`(redacted)/`proposal_snapshot`/`corrected_snapshot`/`outcome`/`authority`/`retrieval_eligible`/`expires_at`. Writer `agents/correction_store.py:127-184`. In `_PURGE_ORDER` (`dsr_purge.py:276`). | **PARTIAL** — see §1 field 4. `corrected_snapshot` and `outcome` are ahead-of-consumer columns with **no writer**. |
| `owner_notifications` (mig 150) | Yes | One row per owner-facing send, `message_sid` + async callback status (`pending/accepted/delivered/failed`), `not_required_reason`. No body, no phone. In `_PURGE_ORDER` (`dsr_purge.py:261`). | **CAPTURED** for the owner-comms outcome. |
| `manager_tasks` (mig 151) | Yes | `objective`/`acceptance_criteria` (redacted JSONB), `status` state machine, `evidence_refs`, `idempotency_key`, `version` CAS, + mig 165 `plan_revision`/`terminal_outcome`/`owner_notification_status`. **MUTABLE** (status/version updated in place). | **CAPTURED as state; NOT append-only.** The append-only history of a task lives in `tm_audit_log`, not here. |
| `manager_task_steps` (mig 152 + 165) | Yes | Ordered plan, `evidence_kind`+`evidence_ref` by-value pointer, `status` incl. `'superseded'`. | **CAPTURED** — and the supersede semantics are the closest thing to field 6 that exists. |
| `campaign_messages` (mig 049) | Yes | tenant/customer/campaign/idempotency_key/message_sid/`send_status`/`message_type`. `campaign_id` **is now populated** (VT-740, `agent/tools/send_whatsapp_template.py:476, 489-506`) — the "never populated" comments at `prod_workflow_diagnosis.py:78` and `agents/send_frequency.py:20` are **stale**. | **PARTIAL** — no delivery state, no read/click, no authority, and **not in `_PURGE_ORDER`** (§7.2). |
| `campaign_recipients` (mig 045) | Yes | `(campaign_id, customer_id, tenant_id, added_at)`, same-tenant composite FKs. | **CAPTURED** as cohort truth; it is what `attribution_writer` joins on. |
| `decision_evidence_links` (mig 183) | Yes | Which card versions were retrieved/selected/rejected at which stage with scores. **`observed_outcome_code` / `observed_outcome_score` / `observed_at` exist and have NO writer** — `_EVIDENCE_LINK_SQL` (`knowledge/card_serving.py:827-834`) omits all three. In `_PURGE_ORDER` (`dsr_purge.py:141`). | **PARTIAL** — retrieval side captured, outcome side is an empty column set. VT-744 says do not duplicate VT-725; agreed — but note VT-741 §Why-deterministic already records that this substrate has not delivered. |
| `o8_tenant_evidence` | **Not a table.** | Mig `183_vt709_o8_tenant_evidence.sql` creates `decision_evidence_links` + `knowledge_incidents`. The brief's item name is a filename. | n/a — resolved to the two tables above. |
| `conversation_log` (mig 164) | Yes | **Owner↔system only** — `role CHECK IN ('owner','assistant')`, verbatim text (4096 cap, `conversation_log.py:42`), `message_sid`, `surface`. Insert-only policies, no UPDATE/DELETE policy (mig `164:56-59`) ⇒ **append-only by construction**. In `_PURGE_ORDER` (`dsr_purge.py:289`). Owner inbound recorded early and pre-gate at `runner.py:60-83`. | **CAPTURED** for owner turns. **Customer↔tenant conversation is NOT in it** and is not stored anywhere as content. |
| `debug_events` (mig 146) | Yes | Failure-class rows: `failure_type`/`component`/`operation`/`error_message`(redacted)/`impact`/`vendor`/`latency_ms`, correlated to `tm_audit_log` by `trace_id`. Deny-all RLS + operator SELECT back-filled by mig `147:133-143`. In `_PURGE_ORDER` (`dsr_purge.py:257`). | **CAPTURED** for the failure half of outcome. |
| `pipeline_runs` / `pipeline_steps` (mig 005/006 + 025) | Yes | Run envelope + per-step `input_envelope`/`output_envelope`/`rationale`/`error_envelope`/cost. Both in `_PURGE_ORDER` (`dsr_purge.py:290-291`). | **CAPTURED** — this is the reasoning depth `tm_audit.reasoning_ref` points at. |
| `send_idempotency_keys` (mig 049) | Yes | `(tenant, idempotency_key)` unique, `customer_id`, `message_sid`, `send_status IN ('sent','window_closed','rate_limited','error')`. Read by the frequency gate (`agents/send_frequency.py:99-120`). | **CAPTURED** as attempt ledger. **Not in `_PURGE_ORDER`** (§7.2). |
| `wa_conversations` (mig 070) | Yes | `(tenant_id, phone_token)` PK, `intro_sent_at`, `last_inbound_at`. **Mutable upsert**, no event history, no FK. | **PARTIAL** — proves *a* reply happened, never *which message* it answered. **Not in `_PURGE_ORDER`** (§7.2). |
| `agent_customer_contacts` (mig 127 + 161 + 200) | Yes | One row per real agent send: agent, draft/batch, template, **`autonomy_level`**, `message_sid`, `delivery_status IN ('delivered','read','failed','undelivered')`, `delivery_updated_at`. In `_PURGE_ORDER` (`dsr_purge.py:167`). | **CAPTURED — the best-shaped table in the inventory.** It is the model the campaign path should be brought up to, not a table to duplicate. |

**Also found and load-bearing, not on the brief's list:** `owner_message_audit` (mig 135 — exact rendered outbound text per sent draft, one row per draft, unique index, in `_PURGE_ORDER`); `llm_call_events` (mig 173 — per-call model/tier/tokens/cost); `pending_approvals` (mig 052 — `decision`, `owner_message_sid`, `resolved_at`; cascades to `pipeline_runs` so DSR reaches it); `ops_audit` (mig 074 — VTR action log); `manager_asserted_facts` (mig 187 — what the Manager told the owner, with supersede/retract).

---

## 3. THE ONE JOIN THAT ALREADY WORKS AND SHOULD NOT BE REBUILT

The owner's exact words on **any** approval — campaign or agent — are already durable:

`pending_approvals.owner_message_sid` (mig `052:62`, set by `mark_approval_resolved`,
`agent/approval_resume.py:484-491`) → `conversation_log.message_sid` (unique per tenant, mig `164:46-47`;
the owner inbound is written *before any gate can consume it*, `runner.py:60-83`).

So even where `agent_corrections` does not fire, **the owner's edit prose is recoverable by a two-column
join.** What is missing is not the text — it is the *link from that text to the proposal it corrected*.
That reframes gap §5.1 from "capture the owner's words" (already done) to "bind the words to the
artifact" (cheap).

---

## 4. WHAT IS ALREADY APPEND-ONLY, AND WHAT ONLY LOOKS IT

| Property | Reality |
|---|---|
| `tm_audit_log`, `conversation_log` | Append-only **for `app_role`** — INSERT policy only, no UPDATE/DELETE policy (mig `147:64-69`, `164:56-59`). |
| `agent_corrections`, `manager_tasks`, `owner_notifications`, `campaign_messages` | Have **full four-verb tenant policies including UPDATE and DELETE** (e.g. mig `154:49-56`, `150:53-61`, `049:96-104`). Immutability is a convention, not a constraint. |
| Everything | The service pool has **BYPASSRLS** (`dsr_purge.py:33-39`), so RLS is inert on that path regardless. |

**Implication for VT-744's "APPEND-ONLY and immutable" constraint:** if the row wants immutability
*enforced* rather than *observed*, the pattern to copy is `conversation_log`'s — grant INSERT+SELECT and
deliberately omit UPDATE/DELETE. Half the inventory does not do this today.

---

## 5. RANKED GAPS WORTH CLOSING — each against the moat gate

> **The gate: "would a better model give this to a competitor for free?"**

### 5.1 — Bind the owner's EDIT to the artifact it edited, on every approval type
**Moat gate: NO. A frontier model cannot produce this. It is one specific human correcting one
specific output for one specific business.** VT-744 calls an edit "the highest-value row in the entire
ledger" and it is right.

Three concrete, small pieces, all in code that already exists:
1. `agents/approval_glue.py:283-284` returns `None` for `approval_type != 'agent_customer_send'` — so
   **`campaign_send` approvals record nothing.** The correction store is keyed on `agent_draft_batches`,
   which a campaign approval has none of; the fix is a second capture site keyed on the campaign, not a
   loosening of this guard.
2. `corrected_snapshot` has **no writer** (`correction_store.py:138`) — an edit records the *before* and
   never the *after*. Exit gate (c) fails on this today.
3. The `timeout`/`defer` branch (`approval_glue.py:456-482`) records no correction — **"ignored" is
   invisible** in the store, even though it is one of the four reactions the row names.

### 5.2 — Authority as a first-class field on every effect, with a `vtr` value
**Moat gate: NO — but weakly.** A competitor can invent an authority enum trivially. What they cannot
get is *our history of which decisions we let run autonomously and what happened*. The enum is cheap;
the accumulated distribution is the asset — and it is unrecoverable if not stamped at write time.

Today: `autonomy_level IN ('L2','L3')` on `agent_customer_contacts` only (mig `127:16`), **nothing on
`campaign_messages`**, and **no `vtr` value anywhere**. VT-744 is right that "authority is the field
most often lost." It is currently lost on the dominant path.

### 5.3 — Reject a VTR override that carries no reason
**Moat gate: NO. A human disagreeing with the machine on a real case is the most concentrated
proprietary signal in the system, and the reason is the entire lesson.**

Today it is optional in three places at once: `api/ops_vtr_console.py:102` and `:108`
(`reason: str = ""`), and `ops_audit.detail TEXT NULL` (mig `074:20`). Exit gate (d) is a two-line
change at the Pydantic boundary plus a `CHECK (btrim(reason) <> '')`. **Cheapest high-value item on the
list.**

### 5.4 — Give the campaign send path the outcome dimension the agent path already has
**Moat gate: NO. Per-tenant, per-customer engagement history is never free.**

`reconcile_customer_send_delivery` updates **only** `agent_customer_contacts` (`customer_send.py:1031`).
A campaign/template send therefore has **no delivery state, no read state, no failure state** — only
`campaign_messages.send_status='template_sent'`, which is transport acceptance. The reconciler already
exists and the `'read'` upgrade rule is already correct (`customer_send.py:979-987`); this is extending
one UPDATE's reach, not building a subsystem. **Note the dependency direction: VT-741 owns the click
and reply signals and is unbuilt — VT-744 must not build them.** But the delivery/read half is shipped
and is simply not wired to half the sends.

### 5.5 — The rejected alternative (field 6)
**Moat gate: NO — this is the only genuinely NEW capture in the row.** Outcomes without
counterfactuals can only ever teach "what we did," never "what was better." Nothing captures it today
(§1 field 6), and the one adjacent record — `emission_gate._emit_blocked_audit` — deliberately stores
only a sha256 (`agent/emission_gate.py:1039-1045`).

**Ranked last deliberately.** It is the only item requiring the Manager to *emit* something it does not
currently emit, which means a prompt/seam change on the turn path — the highest-risk, lowest-certainty
item in a row whose whole virtue is that it is cheap and side-effect-free. If it is built, it belongs
in `tm_audit_log.decision` on an existing `decides` emission, not in a new table.

---

## 6. A CONFLICT INSIDE VT-744'S OWN CONSTRAINTS — resolve before writing schema

VT-744 §Binding constraints: *"Never on the critical path of a turn. Capture failure degrades to a
logged miss; it must NEVER fail a customer send… Fail-open on capture, fail-closed on effects."*
Exit gate (e) requires proving a forced capture failure does not fail the turn.

**The shipped `tm_audit` ACTION layer is deliberately the opposite.** When a `conn` is passed, the emit
is **FAIL-CLOSED — the caller's transaction rolls back if the audit insert fails**:

- `observability/tm_audit.py:168-173` — *"FAIL-CLOSED: caller owns the transaction; redaction,
  param-build AND the insert may all raise → the caller's transaction rolls back (can't-audit ⇒
  can't-act, the VT-460 rails analog)."*
- mig `147_vt514_tm_audit_log.sql:13-18` — *"The ACTION layer emits INSIDE the caller's transaction
  (fail-closed) so a DB-transactional side-effect cannot commit without its audit row."*
- It is enforced by a test named for the property:
  `tests/agent/test_tm_audit_nonbypassability.py` (referenced `tm_audit.py:23`).

These are two defensible designs and they are mutually exclusive on the same write. **A decision is
needed:** either (a) VT-744's ledger is a *new* fail-open store that sits beside the fail-closed spine,
or (b) VT-744 reuses the spine and its fail-open constraint does not apply to action-layer rows. Writing
a schema before this is settled produces exactly the kind of confident-and-wrong artifact this row was
created to avoid. **Flagging to Fazal, not deciding it here.**

The `conn=None` path is already fail-soft and never raises (`tm_audit.py:175-192`), as is
`record_correction` (`correction_store.py:183-184`, SAVEPOINT-isolated) and `load_batch_draft_snapshot`
(`correction_store.py:107-111`) — so the fail-open half of the contract is already demonstrated in
three places.

---

## 7. DEFECTS FOUND — NOT FIXED (out of lane: read-only)

### 7.1 `customers` is exported on a DSR but never PURGED — raw phone and email survive erasure
`customers` holds `display_name`, `phone_e164`, `email` (mig `045:25-31`). It is in the DSR **export**
inventory (`dsr_export.py:45`) but **absent from `dsr_purge._PURGE_ORDER`** (full extract of
`dsr_purge.py:129-311`; `grep 'customers' dsr_purge.py` returns only an unrelated comment at line 296).
Its only FK is `tenants ON DELETE CASCADE` — and DSR **anonymizes** the tenants row rather than deleting
it (`dsr_purge.py:53-58, 108-124`), so the cascade never fires.

**This is the exact scar VT-744's own constraints name** ("DSR anonymizes the `tenants` row… so no FK
cascade will ever clean this up"), sitting on the single most PII-dense table in the schema. It is
pre-existing and unrelated to anything VT-744 will add, but **exit gate (f) cannot honestly pass while
it stands** — a DSR canary that purges a tenant and finds `customers` rows intact is a failed canary.
Recommend its own row, Critical.

### 7.2 Four more tenant tables outside `_PURGE_ORDER` with the same never-fires-cascade shape
- `campaign_messages` (mig `049:66-84`) — `campaign_id`-scoped rows cascade from `campaigns` (which *is*
  purged), but **freeform/agent rows have `campaign_id IS NULL`** and only the tenants FK, so they
  survive.
- `send_idempotency_keys` (mig `049:22-35`) — tenants FK only. Holds `customer_id` + `message_sid`.
- `wa_conversations` (mig `070:13-20`) — **no FK at all**. Holds `phone_token`.
- `escalations` (mig `073:13-26`) — **no FK at all**. Holds free-text `notes`.

Lower severity than 7.1 (tokenised or id-only), but the same class.

### 7.3 Stale comments that would mislead the next reader
`prod_workflow_diagnosis.py:78` and `agents/send_frequency.py:20` both assert
"`campaign_messages.campaign_id` is never populated." **VT-740 fixed that** —
`agent/tools/send_whatsapp_template.py:476, 489-506` writes it and `:481-485` documents the fix. Also
`agent/tools/match_transactions.py:30` says no `attributions` writer exists; VT-563 built one
(`billing/attribution_writer.py:74`). Both are comment-only; I changed nothing.

---

## 8. WHAT I DID NOT DO, AND WHY

- **No schema, no migration, no VT-id, no migration number.** Step 1 is the audit; the brief says do
  not build until it is done, and §6 identifies a conflict that must be resolved before a schema is
  meaningful.
- **No code changed**, so `ruff` / `pytest` had nothing to run against — the only file written is this
  markdown. The dep-less-suite rule did not apply (no test added).
- **Did not verify against a live database.** Every verdict is from migration DDL + the writer/reader
  code path. Where a table exists but has no writer I said so and cited the absent call site
  (`corrected_snapshot`, `observed_outcome_code`, `authority='vtr'`) rather than inferring from row
  counts I cannot see.
- **Did not fix §7.1** despite it being a live DPDP hole — out of this lane's stated surface (one file),
  and it deserves its own row and its own canary rather than a drive-by.
