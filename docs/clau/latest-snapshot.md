# Latest State Snapshot

**As of:** 2026-06-02 (regenerated; reconciled against `git log --oneline` on main + `gh pr list`). **Main HEAD:** `4725539` (VT-208 Shopify client_credentials, #221). **Reports-Jun15 in 13 days.**

> Treat as suspect until reconciled (Rule #14). Three docs/identity PRs (#222/#223/#224) were open at regen time — confirm their merge state against `gh pr list` before trusting "IN FLIGHT".

---

## CRITICAL PATH

**Reports-Jun15 gate.** The Sprint-3 **ingestion engine is COMPLETE on main**: primitives (VT-52 vision / VT-53 clarify / VT-54 dedup), the **two-surface transaction model** (273 `customer_ledger_entries` + 276 `imported_transactions`), 258 read-wire, **275 attribution bridge** (tentative-suggest, no auto-ledger-write), methods 55/56/57/58/59/63 + Apify 61/62. Vision (Anthropic) + voice (Sarvam saarika:v2.5) **live-verified**. Shopify: VT-208 client_credentials merged (#221) + VT-213 live walk GREEN against the own dev store. VTR human-on-the-loop locked (CL-426).

Current feature in build = **VT-267 owner onboarding** (+ VT-268 guardrail enforcement). Launch-blocker remains **VT-231** (prod Supabase Mumbai; CL-422 — no real customer data on dev until it closes; Fazal-side, parked).

## IN FLIGHT (CC)

Three PRs awaiting Pillar-7 task-merge (all green / docs 0-CI):
- **#224 — VT-267 PR-A2** (D1 tenant identity): mig 066 unique `whatsapp_number` (business_contact) + `owner_contact` nullable + `create_tenant_if_unknown` (merge-on-same-number). Supersedes CL-76 DC2 (flagged).
- **#223 — CL-427 connector-audit** (docs): CL-421 "Shopify conforms" correction + standing connector-audit gate + VT-283/284/285 rows.
- **#222 — VT-213 → Done** (docs flip, Shopify walk green).

Plans posted, awaiting Cowork review: **VT-283** (Shopify owner-facing OAuth managed-install — the production zero-paste path; the dev store is same-org so a separate merchant store is needed to live-test), **VT-85/8.5** (consent-capture: record_of_consent tokenized + opt-out + DSR + versioning, unblocks VT-60).

VT-267 **PR-B** (intent `first_data_step_onboarding` + `method_selector` + floor state machine) pending CC's 4 build-confirms (classify v-bump governance / method_selector model / floor timers / machinery-scope). Then **PR-C** (in-app-browser web wizard, magic-token), **PR-D = VT-268** (guardrail enforcement: discount→request_owner_approval, accounts-book→structural no-write).

## BLOCKED ON

- **Cowork:** task-merge #222/#223/#224; review the VT-283 + VT-85 plans; tick the 4 PR-B confirms; draft the privacy notice (CL-425 owner_inputs basis; sub-processors Anthropic/Sarvam/Twilio/Voyage/Supabase/Apify; CL-416 retention; DPDP) + the QR consent text (Fazal legal-validates, RKeCom Services OPC Pvt Ltd).
- **Fazal:** VT-231 Mumbai prod (parked); legal-validate the privacy/consent copy. (D1 identity = RULED; Shopify scopes = reinstalled, walk green.)

## NEXT ACTION

- **CC (on signal):** build PR-B once the 4 confirms land; build VT-85 on plan-approval; build VT-283 on plan-approval; PR-C then PR-D.
- **Cowork:** merge the 3 open PRs, tick PR-B confirms, draft privacy + consent copy, VT-189 Ops Console wireframes.

## DO NOT

- Merge an **owner-facing** Shopify connector on client_credentials — owners need the OAuth managed-install (VT-283); client_credentials is dev/own-store/same-org only (CL-421 / CL-427).
- Let **real customer data** touch dev pre-VT-231/Mumbai (CL-422). Dev = synthetic only.
- Re-open **PR #61** (VT-172 orphan, closed 2026-06-02).
- Build the privacy/consent **legal copy** in CC — Cowork drafts, Fazal/counsel legal-validates (VT-272 + VT-85 text).
