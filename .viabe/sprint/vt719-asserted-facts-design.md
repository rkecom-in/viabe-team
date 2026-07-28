# VT-719 — S3 asserted-facts ledger: design note

Status: DRAFT (CC, 2026-07-29) — designed WITH O8 §12.3 as one system; build starts after the
VT-718 shadow/enforce proof. Migration number: from Clau (187+ free; 186 = Codex specialist-memory).
Authorization: CL-2026-07-28-single-voice-manager + CL-2026-07-28-o8-living-knowledge.

## 1. What it is
A durable, tenant-scoped record of the facts and commitments the Manager has TOLD the owner —
not what it knows (that's O8), but what it has SAID. Consulted at compose (inside the VT-718
choke seam); the only legal way to flip a prior assertion is an OWNED change ("earlier I said
X — that's now Y because…"). Silent flips are a defect class, not a style issue.

## 2. Schema (ONE migration: table + RLS + FORCE RLS + `_PURGE_ORDER` — the standing rule)

`manager_asserted_facts` (tenant-scoped):
- `id` uuid PK, `tenant_id` uuid NOT NULL (RLS key)
- `asserted_at` timestamptz NOT NULL, `surface` text, `message_sid` text NULL
  (joins the exact outbound that carried it — same key conversation_log uses)
- `fact_key` text NOT NULL — canonical predicate slug (`weekly_report_day`,
  `dormancy_definition`, `recommended_channel`, `trial_terms`, …). THE contradiction join key;
  registry of keys lives in code (typed enum-ish module), not free-text.
- `fact_value` jsonb NOT NULL — typed value for deterministic comparison
- `statement_text` text — the sentence as said (PII-minimized; conversation_log holds the
  verbatim wire anyway)
- `derived_from_card_id` uuid NULL — **the O8 §12.3 join**: FK → `knowledge_cards.id`
  (global table, same DB; card versions are immutable so the FK pins the exact version)
- `derived_from` jsonb — non-card evidence (tool run id, metric window, VTR instruction)
- `status` text: `active` | `superseded` | `retracted`; `superseded_by` uuid NULL (self-ref)
- Index `(tenant_id, fact_key, asserted_at DESC)`; append-mostly (supersession = new row +
  status flip on the old one, never destructive update).

## 3. Write path (staged — deterministic first)
- **Stage 1 (this row):** deterministic writers only — the code paths that make commitments
  already know they are doing so (weekly-report day confirm, dormancy definition, trial terms,
  agent-activation promises, campaign schedule). Each records `fact_key`+`fact_value` at send.
- **Stage 2 (post-S4):** the unified composer records assertions as a compose output
  (structured `asserts:` field in the turn contract), not via NLP-after-the-fact extraction.

## 4. Read path — the contradiction check (inside the VT-718 choke seam)
At compose: latest `active` fact per `fact_key` touched by the draft. Deterministic veto only
(Fazal's no-lists standing): a draft that STATES a different `fact_value` for an active
`fact_key` is returned to the composer with the prior assertion attached — the composer must
either (a) keep the old value, or (b) emit the OWNED-change framing. The check never rewrites
text and never blocks an effect — voice-advisory only, same posture as the S2 dedup.

## 5. The O8 §12.3 supersession sweep (one system, not two)
When a card supersedes/expires/flips assignment (`knowledge_lifecycle_events` row), a sweep
finds `manager_asserted_facts WHERE derived_from_card_id = <old version> AND status='active'`
per tenant and queues an OWNED-change correction through the owner_comms_queue (idle-paced,
VT-683 P2) — the Manager proactively corrects itself instead of waiting to contradict itself.
The sweep is part of THIS row's scope (the join is why the two specs are one system).

## 6. Privacy
Tenant-scoped, RLS + FORCE RLS, `_PURGE_ORDER` registration in the same migration
(children-first: `manager_asserted_facts` before `tenants`). DSR export includes it (it is
"what we told you"). statement_text carries no third-party PII by construction (owner-facing
sentences only).

## 7. Explicitly not here
- No LLM contradiction scoring (S4 may add a shadow judge; the ledger is deterministic).
- No specialist writes — specialists' thin memory is Codex's mig-186 estate; the asserted-facts
  ledger is the MANAGER's voice record only (§13.1 ownership).
