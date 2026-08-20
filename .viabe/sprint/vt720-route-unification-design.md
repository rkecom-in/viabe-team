# VT-720 — S4 route unification: design note

Status: DRAFT (CC, 2026-07-31; written during the VT-721 S4 ×3 window). Build starts only after
VT-721 closes and Fazal sequences it. Clau: audit-after; objections early.

## 1. The casebook (what S4 must structurally kill)
The four persistent full-pack classes — stable across three independent ×3 runs (VT-719 /
VT-722 / VT-721 baselines), all pinned to the 2026-07-06 pack, all pre-dating single-voice:
1. **Enforce populate-first gap** (`efficient_collection_incremental_no_reask`,
   `multi_field_single_message_hinglish`): the narrow enforce gate's status line re-asks facts
   the draft already carries ("which city" with a seeded city) — the gate composes from partial
   context instead of the populate-first substrate.
2. **Correction re-confirm loop** (`owner_corrects_inline_direct`): an inline owner correction
   triggers "is that right" re-confirm instead of acceptance — the correction handler speaks
   with its own template rather than the brain's context.
3. **False 'drafted' claim** (`routing_db_proof_finance_vs_sr`): a finance-routed ask claims a
   draft exists that the DB disproves — a gate's reply template asserts state it never checked.
4. (run-6 residue, structurally the same) template-voiced consent/completion seams.
Common shape: **a gate that OWNS a reply template** speaks without the Manager's context.
S4's thesis: gates CLASSIFY; only the composer SPEAKS.

## 2. Target architecture (incremental, one route at a time — never big-bang)
- **The composer** = the existing manager brain dispatch (already carrying: wire-truth
  conversation block, commitments block, week-plan block, onboarding/in-flight state blocks).
  No new composer is built — S4 is about who is ALLOWED to emit, not a new engine.
- **Classifier contract**: a converted route returns
  `{intent, facts, constraints, urgency, suggested_disposition}` — NEVER `reply_text`. The
  runner hands the classification to dispatch as a per-turn system block
  (`## Turn classification` — deterministic findings the brain must honor, e.g. "the consent
  floor matched DECLINE: you must acknowledge and stop asking").
- **Deterministic floors keep VETO power** (Fazal's no-lists ruling inverted correctly):
  hard-stops (opt-out/DSR/consent-grant-exactness/money gates) still act directly and may
  TERMINATE a turn without the brain; what they lose is the right to SAY things — their
  owner-visible line is composed by the brain from the classifier's facts, through the S2
  choke, against the S3 ledger.
- **Latency guard**: routes converted to classifier+compose gain one LLM turn where they had
  none. Mitigation: per-route conversion only where a template exists TODAY (those already pay
  a send); pure-ack floors (e.g. STOP confirmation — legally fixed wording) stay deterministic
  verbatim, explicitly exempted and listed.

## 3. Conversion order (each = own stage, ×3, revert-over-repair)
1. **Enforce journey gate status line** (kills casebook #1): the gate stops rendering its own
   "still needed" line; it classifies (facts-known/facts-missing from the populate-first draft)
   and the turn-brain composes. Highest value; the gate is already narrow.
2. **Correction handling** (#2): the inline-correction path classifies `correction{field,
   old, new}`; composer acknowledges + continues (no re-confirm template).
3. **Routing acknowledgements** (#3): route-pick emits classification only; the brain's reply
   must ground any "drafted/done" claim in DB reads (the in-flight-state block already carries
   the truth — the false claim dies when the template does).
4. **Consent/completion seams** (run-6 residue): the decline/complete floors classify; the
   composer speaks once (the S2 choke already deduplicates the seam).
Each conversion deletes the route's template constants — a converted route that regrows a
template is a review-visible diff smell, and the S2 CI gate already blocks new raw egress.

## 4. Proof per stage
Targeted casebook scenario ×3 (must flip to 3/3 PASS) + full-pack ×3 (no new classes) +
latency delta on the converted route (p50 turn time before/after, from pipeline_steps).

## 5. Not in S4
No new tables. No prompt-only fixes (deterministic classification, composed voice). No
conversion of pure-ack legal floors (STOP, DSR receipts) — listed exemptions.
