---
vt_id: VT-6
title: VT-IntegrationInvention — 9 ingestion methods + vision-LLM + clarifying flow
status: Done
priority: Critical
sprint: Sprint 3 - Ingestion Methods 1-2
type: Feature
area: [Ingestion]
assignee: Clau
parent: ""
sub_items: [VT-52, VT-53, VT-54, VT-55, VT-56, VT-57, VT-58, VT-59, VT-60, VT-61, VT-62, VT-63]
exec_order: 1
branch: "feat/vt-ingestion"
version: "v1.0"
notion_legacy_id: 356387c2-cc5a-8192-b72c-f29ebb3c5ef5
last_updated: 2026-05-25T03:45:00+05:30
---

# VT-6 — VT-IntegrationInvention — 9 ingestion methods + vision-LLM + clarifying flow

## Why this parent exists
The 9 ingestion methods are the moat. Roughly 70% of Indian SMBs do not run a POS — restricting Team to POS-only ingestion would shrink the addressable market to high-end shops that don't actually need this product. The whole product hypothesis depends on meeting owners where their data already lives: paper books, phone contacts, UPI exports, KOT printouts, cash books, customer-side opt-ins, online listings, and free-text WhatsApp entries. If ingestion is fragile, the customer ledger is empty, the agent has nothing to reason over, and the product fails silently.
This parent owns the whole ingestion surface. Three shared primitives (vision-LLM extraction, clarifying-question flow, dedup/merging) are built first because every method depends on them. Each method then becomes a thin adapter that produces canonical `customer` and `transaction` rows tagged with `acquired_via`. Confidence scoring is consistent across methods: <0.7 triggers clarification, 0.7–0.85 commits with notification, ≥0.85 commits silently.

## What this parent owns
1. Vision-LLM extraction pipeline shared across paper-book, cash-book, and KOT/POS export image methods. Returns structured fields with per-field confidence.
2. Clarifying-question flow that fires when extraction confidence falls below threshold. Owner replies via WhatsApp, system commits resolved data.
3. Dedup and merging logic that resolves the same customer across multiple ingestion methods (e.g., contacts entry + UPI history + paper book all referencing the same person).
4. Method 1: Paper book photograph (vision LLM).
5. Method 2: Phone contacts list import.
6. Method 3: UPI transaction history export (PhonePe, GPay, Paytm PDF/CSV).
7. Method 4: KOT/POS export (where it exists).
8. Method 5: Cash-book photograph + voice note (multimodal).
9. Method 6: Customer-side QR opt-in at checkout (consent flow split between privacy half VT-8.5 and ledger half here).
10. Method 7: Apify scrape of Zomato/Swiggy/Magicpin (context only, no PII).
11. Method 8: Apify scrape of Google Business Profile reviews (context only, no PII).
12. Method 9: Owner-typed natural-language entries via WhatsApp.

## Architectural rules binding every subtask
- Pillar 3 (tenant isolation): every ingested row carries `tenant_id` derived from invocation context. No method accepts `tenant_id` as a parameter.
- Pillar 4 (retrieve, don't calculate): never invent business-type defaults to fill missing fields. Either extract, ask the owner, or leave null with explicit confidence.
- Pillar 8 (no patchwork): no regex scrubs to clean up LLM output. If extraction is wrong, fix the prompt or trigger clarification — never post-process with substitutions.
- Confidence thresholds are uniform: <0.7 → ask, 0.7–0.85 → commit + notify, ≥0.85 → commit silently. No method invents its own thresholds.
- Methods 7 and 8 (Apify scrapes) are context-only. No PII is extracted, ever. Reviews and listing metadata go to L1 KG; customer-identifying data does not.
- Every ingestion writes the source tag (`acquired_via: paper_book | contacts | upi_phonepe | upi_gpay | upi_paytm | kot_pos | cash_book | qr_opt_in | apify_zomato | apify_swiggy | apify_magicpin | apify_gbp | owner_typed`) so downstream observability can attribute per-method failure rates.
- Method 6 (customer QR) requires explicit consent capture with timestamp and consent text version. The privacy half is VT-8.5; this parent owns only the ledger-side write after consent is recorded.

## Subtasks under this parent
1. **VT-6.1** — Vision-LLM extraction pipeline (shared primitive).
2. **VT-6.2** — Clarifying-question flow (shared primitive).
3. **VT-6.3** — Dedup and merging logic (shared primitive).
4. **VT-6.4** — Method 1: Paper book photograph.
5. **VT-6.5** — Method 2: Phone contacts list import.
6. **VT-6.6** — Method 3: UPI transaction history export.
7. **VT-6.7** — Method 4: KOT/POS export.
8. **VT-6.8** — Method 5: Cash-book photograph + voice note.
9. **VT-6.9** — Method 6: Customer QR opt-in (ledger half).
10. **VT-6.10** — Method 7: Apify Zomato/Swiggy/Magicpin (context only).
11. **VT-6.11** — Method 8: Apify GBP reviews (context only).
12. **VT-6.12** — Method 9: Owner-typed entries via WhatsApp.

## Definition of done
- All 12 subtasks Done.
- A canary subscriber onboards using 3+ methods (e.g., paper book + UPI export + contacts) and the dedup logic correctly merges duplicate customers across them.
- Confidence-threshold tests pass for each method: low-confidence extractions trigger clarification flow; medium-confidence commits with owner notification; high-confidence commits silently.
- No method writes PII from Apify scrapes (negative test confirms).
- Source-tag coverage: every ingested row is queryable by `acquired_via`; per-method failure rate dashboards exist.
- A founder using only paper books (no POS, no UPI export) can fully onboard.

## Out of scope
- Knowledge architecture L1-L4 (VT-7) — ingestion writes raw to canonical tables; KG construction is downstream.
- Privacy machinery: typed wrappers (VT-8.1), k-anon (VT-8.3), opt-out flow (VT-8.5), DSR APIs (VT-8.6).
- The agent's reasoning over ingested data (VT-4).
- MCP tools that query the ledger (VT-5.2 `query_customer_ledger`).
- Owner-facing WhatsApp UX for the clarifying flow — this parent owns the backend; owner-facing message templating is VT-9.4.
- Apify actor approvals (VT-13.5).
- Vendor selection for KYC (VT-13.7).

## Branch convention
- Parent branch: `feat/vt-ingestion`.
- Subtask branches: `feat/vt-ingestion-<short>` (e.g. `feat/vt-ingestion-paper-book`, `feat/vt-ingestion-upi-phonepe`).
- PR title format: `<type>(ingestion): <description> (VT-6.N)`.
- Reviewers: CoderC implementation; CoderX must review every method's confidence-threshold logic, every PII-handling boundary, and the dedup logic.
- Merge target: `dev`.

## Status history
- 2026-05-25 03:45 IST: migrated from Notion (notion_legacy_id: 356387c2-cc5a-8192-b72c-f29ebb3c5ef5)
