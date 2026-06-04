# Operating Policies & Conditions — single quick-reference

**Purpose (Fazal's ask, 2026-06-04):** ONE readable, categorized reference of the standing **product / operating
policies + runtime / launch conditions** — "what are the rules" at a glance. Distinct from
[`decisions-ledger.md`](decisions-ledger.md) (the chronological CL list) and
[`latest-snapshot.md`](latest-snapshot.md) (current state). Each rule cross-links its CL/Pillar source of truth.

**Owner: Cowork** — keep current as new policies land. Don't re-litigate anything here; if a rule changes, the
change goes through a CL first, then this doc reflects it.

---

## 1. Memory / personalization

- **Dormancy = brain-decided, business-type-aware (VT-312).** No fixed global "X days dormant" threshold. The
  Composer surfaces the tenant's OWN customer **recency distribution** + business_type into the bundle; the
  brain judges who is dormant for THAT tenant/business-type at reasoning time.
- **High-value = brain-decided, per-tenant + business-type (VT-312).** From the tenant's own customer **spend
  distribution** (percentiles surfaced into the bundle); the brain decides what's high-value for that tenant.
- **BINDING GUARDRAIL — L3 cohort `recency_band` stays FIXED + coarse.** The cross-tenant k-anon cohort band
  (`0_30d`/`30_60d`/`60_90d`/`90d_plus`) is a CONSISTENT, fixed, time-based definition computed at nightly L3
  construction — it can NEVER be parameterized per-tenant (k-anon cohorts can't aggregate if "dormant" means
  something different per tenant). Brain decisions are **per-tenant personalization ONLY** and must not leak
  into the cross-tenant band. The two planes stay separated.
- **L4 skill corpus is HUMAN-authored, version-controlled, NOT LLM-generated (VT-313; Pillar 4/5).** Edits go
  through PR review. Improvements happen via prompts + retrieval, never fine-tuning (Pillar 5).
- **Four knowledge layers:** L1 per-tenant entities/relationships · L2 per-tenant episodic · L3 cross-tenant
  k-anon priors · L4 skills corpus → one composition (`build_sales_recovery_context`, Pillar 8).

## 2. k-anonymity (Pillar 6)

- **k = 10, LOCKED (CL-28, Type-3).** Cannot be lowered without board approval.
- **Enforced over the set of CONTRIBUTING tenants** (≥10 tenants that actually contributed to a cohort), NOT
  over attribute-matchers. At-rest invariant: `l3_patterns.n_tenants >= 10` CHECK + the admission gate at
  construction; non-bypassable in prod even with admin credentials.
- **k-anon is BUILD-TIME, not runtime.** Coarsen first (ward/city-tier), then admit only if k≥10 — never write
  per-individual data and check k afterward.

## 3. Consent / privacy

- **Gate ALL business-initiated sends on opt-in.** No unsolicited customer messaging.
- **Brain transmit gated on `owner_inputs` consent (CL-425).** The brain transmits the owner's inbound body
  (may carry customer PII) to Anthropic only when `tenants.owner_inputs` is true (fail-closed on unknown).
- **No raw PII in KG / L2 / L3 / outbox / logs (CL-390).** Phone hashed at rest; bodies redacted at the
  persistence boundary (forward: VT-144; historical backfill: VT-153/mig 090). Templated, token-based L2
  summaries — never raw text.
- **Retention = lifetime-of-relationship (CL-416).** DSR-purge is the SOLE deletion path. Two distinct rights:
  - **DSR delete (CL-416):** hard-delete subject data across all inventoried tables (FK-safe order) + scrub
    EVERY identifying tenant column irreversibly to NULL (business_name/whatsapp_number/owner_phone/
    owner_contact/locality — VT-160). The `WHERE tenant_id` predicate is the SOLE scoping surface on the
    BYPASSRLS purge path, guarded against silent removal (VT-154).
  - **Opt-out reconstitution (VT-76):** 7-day sentinel-null anonymization of the customer's L2 footprint
    (`referenced_entity_id` → all-zeros sentinel) — KEEPS the event row (audit + k-anon integrity), NOT
    deletion. 8-day SLA breach = critical alert (`reconstitution_sla_breach`).
- **VTR sees de-identified / business-level data only — customer PII is ENCRYPTED FROM THE VTR (CL-426).**
  Identity-needing escalations route to the OWNER, not the VTR.
- **Alert trigger kinds: the DB CHECK must stay synced to the code Literal (CL-428)** — any new kind = both
  the `TriggerKind` Literal AND a CHECK-extending migration in the same PR.

## 4. Residency (DPDP data-localization)

- **Dev = Seoul (`ap-northeast-2`), SYNTHETIC data ONLY, until VT-231 (CL-422).** Accepted with a launch-gate
  sunset — do NOT re-flag Seoul as a DPDP issue.
- **Prod = Mumbai (`ap-south-1` primary + `ap-south-2` backup)** — VT-78 / VT-231 (Fazal/infra).
- **NO real customer data touches dev until VT-231 closes.** Hard constraint.

## 5. Gate-live conditions (what gates WHAT)

- **Reports-Jun15** runs on business reporting — NOT gated by any of the customer-data items below.
- **Customer-data-go-live** gated on: **VT-78** prod Mumbai residency · **VT-231** prod Supabase.
- **Customer-messaging-go-live** gated on: **VT-318** inbound STOP-handler (WABA) · display-name/WABA approval
  · **VT-156** published privacy notice (Fazal/counsel).
- Full live-tracker: [`.viabe/customer-data-go-live-prereqs.md`](../../.viabe/customer-data-go-live-prereqs.md).
- **Connectors:** every connector must pass the zero-paste audit — owner ONLY approves, no app-creation / scope
  screen / secret paste / dev step — BEFORE it reaches owners (CL-421 / CL-427).

## 6. Engineering discipline

- **Canary mandatory (DR-15 / Rule #15).** Every row touching an external API / SDK / persistence ships a
  real-call canary — real request, verify response, fail-not-skip. No skip-theatre (e.g. VT-314 wired the real
  voyage call into CI).
- **Allocators, always (CL-424).** VT-IDs via `scripts/vt_id_allocate.py`; migration numbers via
  `scripts/migration_id_allocate.py` (flock-serialized). Allocate ONCE up-front before any parallel phase —
  never hand-pick or race. One coherent PR per numeric VT row.
- **Pillar-7: Fazal-authorized merges.** Every merge requires explicit authorization; session-blanket auth is
  grant-scoped, not perpetual. Route via PR; never push to main; never auto-merge.
- **Pillar 8: no patchwork.** Fix the architecture (the wrapper, the construction logic), not the symptom.
- **Pillar 3: tenant isolation, structural.** Three independent layers (RLS + typed wrappers + agent context
  isolation); bypassing any one must not leak. Cross-tenant reads only via the sanctioned, audited service-role
  paths (k_anonymity, l3_construction, reconstitution scan).
- **Pillar 1: deterministic vs reasoning split.** The pre-filter / phase-machine / scheduled deterministic
  triggers contain NO LLM calls (CI-enforced).
- **Source of truth is local files** (`.viabe/sprint/`, `docs/clau/`), NOT Notion (read-only archive). Memory
  is never authoritative — reconcile every status against `git log` + the board (Rule #14).

---

*Seeded 2026-06-04 from `decisions-ledger.md` + this session's rulings (VT-312 brain-decides, VT-76/154/160/153
DSR hardening, VT-314 voyage, Phase-1 close). Cross-links point to the CL/Pillar source of truth — update those
first, then reflect here.*
