---
vt_id: VT-8
title: VT-PrivacyArchitecture — typed wrappers, k-anon, opt-out, DSR, residency
status: Deferred
priority: Critical
sprint: Sprint 7 - Knowledge Architecture
type: Feature
area: [Privacy, Database, Legal/Policy]
assignee: Clau
parent: ""
sub_items: [VT-72, VT-73, VT-74, VT-75, VT-76, VT-77, VT-78, VT-79, VT-80, VT-144, VT-145, VT-147, VT-148, VT-149, VT-150, VT-151, VT-152, VT-153, VT-154, VT-156, VT-158, VT-160, VT-161]
exec_order: 2
branch: "feat/vt-privacy"
version: "v1.0"
notion_legacy_id: 356387c2-cc5a-812d-8062-e895eabd76fc
last_updated: 2026-05-25T03:45:00+05:30
---

# VT-8 — VT-PrivacyArchitecture — typed wrappers, k-anon, opt-out, DSR, residency

## Why this parent exists
DPDPA 2023 is now enforceable in India. A privacy incident in Year 1 ends the company. Reports product handles no PII (it works on retail-location data, not customer data). Team handles dormant-customer ledgers — the most sensitive consumer data the company has ever processed. Privacy cannot be retrofitted; it must be enforced at every architectural boundary from day one.
This parent owns the three independent enforcement layers Pillar 3 demands (Postgres RLS from VT-2.4 is the first; this parent adds the second and third), the build-time k-anonymity invariant Pillar 6 demands, the data subject rights APIs DPDPA requires (export, correction, deletion), the data residency configuration that keeps everything in India, and a breach-detection runbook that must exist before launch.

## What this parent owns
1. Typed application wrappers around every PII-touching query. Lint rule blocks raw SQL access to PII tables. Wrappers stamp `tenant_id` from invocation context.
2. Agent context isolation: agent invocation cannot read across tenants even with elevated DB credentials. Enforced at the orchestrator/agent boundary (VT-3.4 and VT-4).
3. K-anonymity admission gate: every L3 write checks k≥10 at construction. Failures block the write and log the attempt. Cannot be disabled in production.
4. Locality coarsening: ward (urban) and city-tier (rural) lookup tables. Used at L3 construction.
5. Customer opt-out flow + reconstitution: customer can opt out via the QR Method 6 surface. Owner-verified identity reconstitution within 7-day SLA per Pillar 7.
6. DSR APIs: data export (JSON dump within DPDPA SLA), correction (owner-mediated), deletion (hard delete with crypto-shred of embeddings).
7. Data residency configuration: ap-south-1 (Mumbai) primary, ap-south-2 (Hyderabad) backup. No data leaves India. Verify via DB connection log inspection.
8. Breach detection rules + runbook: who to notify (DPDPA, customers), within what timeline, what to log.
9. Privacy audit log table: every PII access logged with actor, query, rows touched, timestamp.

## Architectural rules binding every subtask
- Pillar 3 (tenant isolation, structural): three independent enforcement layers must exist. Postgres RLS (VT-2.4), typed wrappers (VT-8.1), agent context isolation (VT-8.2). Bypassing any one must not produce a data leak.
- Pillar 6 (k-anon build-time): the admission gate is non-bypassable in production. Even with admin credentials, you cannot insert an L3 pattern with k<10. CI tests verify the invariant.
- Pillar 7 (owner is source of truth on identity): customer reconstitution after opt-out requires owner-verified identity within 7 days. Automatic reconstitution from synthetic-to-real ledger entries is forbidden.
- Pillar 8 (no patchwork): privacy fixes route through architecture, not regex scrubs. If PII is leaking through a query, fix the wrapper and the lint rule, not the response post-processing.
- Every PII-touching code path has a corresponding negative test: if the wrapper is bypassed, the lint or runtime gate must fail.
- Every privacy audit log entry is immutable: append-only table, no updates allowed.
- Customer DSR requests have measurable SLAs documented in the runbook and tested in CI via synthetic flow.

## Subtasks under this parent
1. **VT-8.1** — Typed application wrappers + lint rule.
2. **VT-8.2** — Agent context isolation.
3. **VT-8.3** — K-anonymity admission gate (build-time invariant).
4. **VT-8.4** — Locality coarsening (ward/city-tier).
5. **VT-8.5** — Customer opt-out + reconstitution (7-day SLA per Pillar 7).
6. **VT-8.6** — DSR APIs (export, correction, deletion).
7. **VT-8.7** — Data residency configuration verification.
8. **VT-8.8** — Breach detection rules + runbook.
9. **VT-8.9** — Privacy audit log table + write path.

## Definition of done
- All 9 subtasks Done.
- Cross-tenant attack tests pass at all three enforcement layers (RLS, typed wrappers, agent context).
- K-anon admission gate test: synthetic L3 write with k=9 fails with structured error; k=10 succeeds.
- Opt-out flow: customer opts out via QR; ledger entry is hidden from agent retrieval within minutes; reconstitution works only with owner-verified identity within 7 days.
- DSR export: synthetic customer DSR returns full JSON dump within DPDPA SLA. Correction flow updates fields with audit trail. Deletion crypto-shreds embeddings (verified by post-deletion retrieval test returning zero results).
- Data residency: DB connection logs inspected; all dev and prod connections originate from and terminate in ap-south-1/ap-south-2.
- Breach runbook reviewed by Fazal; tabletop exercise completed.
- Privacy audit log written for every PII-touching query in a synthetic run; log is append-only (UPDATE rejected).

## Out of scope
- Postgres RLS itself (VT-2.4).
- L3 pattern construction (VT-7.5) — but the admission gate this parent owns IS used by 7.5.
- Method 6 (customer QR) ledger half (VT-6.9) — but the privacy half (consent capture) IS this parent's VT-8.5.
- DPDPA legal counsel review (VT-13.10).
- Vendor selection for KYC (VT-13.7).

## Branch convention
- Parent branch: `feat/vt-privacy`.
- Subtask branches: `feat/vt-privacy-<short>` (e.g. `feat/vt-privacy-typed-wrappers`, `feat/vt-privacy-k-anon-gate`).
- PR title format: `<type>(privacy): <description> (VT-8.N)`.
- Reviewers: CoderC implementation; CoderX must review every privacy-touching change. Fazal must approve VT-8.5 (opt-out), VT-8.6 (DSR), VT-8.8 (breach runbook) personally.
- Merge target: `dev`.

## Status history
- 2026-05-25 03:45 IST: migrated from Notion (notion_legacy_id: 356387c2-cc5a-812d-8062-e895eabd76fc)

## DEFER (2026-06-03, Cowork Sprint 7 reconcile)
Parent privacy arch. Built: consent (VT-8.5), DSR deletion (VT-8.6 / dsr_purge.py), audit-log schema (mig 008). Unbuilt: typed wrappers, agent isolation, write-side k-anon gate, locality, residency, breach runbook. Parent DoD = all 9; NOT Jun15-blocking.
