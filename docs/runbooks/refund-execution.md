# Runbook — Refund execution + 30-day graceful exit (VT-93)

## What it is
`billing/refund_executor.execute_refund(tenant_id, refund_reason)` is the single
refund execution path (Pillar 8). Full refund of `subscriptions.cumulative_fees_paid_paise`
+ Razorpay subscription cancel + phase → `refunded` + a 30-day read-only graceful
exit. Idempotent on `(tenant_id, refund_reason)`.

`refund_reason ∈ {day39_eligibility, manual_request}`.

## Who calls it
- **VT-85** (refund-conversation): on the owner's **REFUND** reply. *This is the
  production caller.* VT-93 ships the mechanism; VT-85 wires the trigger.
- Manual ops (`manual_request`): the Telegram-command trigger is a follow-up
  (NEEDS-FAZAL bot command); the executor path exists today.
- The day-39 auto-sweep does **not** call this — auto-refunding without owner
  consent contradicts the offer model; VT-85 converts the day-39 auto-transition
  into the offer.

## State machine (`refund_executions`, PK `(tenant_id, refund_reason)`)
```
pending → refunding → ┬→ completed                     (refund + cancel ok)
                      ├→ partial_failed                (a refund call failed; HALT)
                      └→ pending_subscription_cancel   (refunds ok, cancel failed)
```
- `completed` is **immutable** (migration-099 trigger blocks UPDATE/DELETE for
  every role except the DSR purge session).
- Ordering is phantom-refund safe: money moves first, phase flips to `refunded`
  only after refund+cancel succeed, the row freezes `completed` last.

## NEEDS-FAZAL (fail-closed today)
- **Razorpay live keys + cutover (VT-89).** `billing/razorpay_refund.py` default
  client REFUSES every call (no money moves on an accidental deploy). The live
  client drops in behind the `RazorpayClient` protocol. Per-payment refunds need
  VT-89's charge ledger; until then the executor refunds the running total as one
  call.
- **Templates `refund_processing` / `refund_completed`** carry null Meta SIDs
  (Pillar 7 — Fazal reviews every word). A refund still COMPLETES with a null SID;
  the row records `notification_pending=true` and Fazal gets a Telegram alert
  (a notification gap is not a refund failure).

## Failure handling (no auto-retry)
- **partial_failed** — a refund call failed mid-stream; remaining refunds NOT
  attempted. Fazal alert. Investigate via `refund_responses` JSONB (per-step
  results). Recovery is manual (a fresh `manual_request` execution refunds the
  outstanding balance once the data issue is fixed).
- **pending_subscription_cancel** — refunds succeeded, cancel failed. Re-invoking
  `execute_refund` RESUMES from the cancel step — it skips the already-succeeded
  refund (guarded on the per-step `refund_responses` ledger, so no double-refund)
  and completes once the cancel succeeds. The automatic retry sweep lands with the
  live caller (VT-85/VT-89); until then recovery is a manual re-invoke (Fazal is
  alerted).

**Idempotency / resume:** `execute_refund` is safe to re-invoke. The advisory
lock + `INSERT ON CONFLICT` + `SELECT FOR UPDATE` serialize the claim; on re-entry
the per-step `refund_responses` ledger means each external step (refund, cancel)
is attempted at most once. The deterministic Razorpay idempotency key is the
final vendor-side backstop for the concurrent-duplicate window a DB transaction
cannot span (it can't be held across an external HTTP call).

**Event taxonomy:** `refund_executed` is the terminal audit event (pipeline_log +
immutable privacy_audit_log). The phase-machine event `day39_refund_triggered`
keeps its original meaning (fires the `->refunded` transition); it is NOT
overloaded to mean "executed" (Cowork PB1).

## Privacy
- Every execution + status change appends to the immutable `privacy_audit_log`
  hash-chain (`refund_executed` / `refund_partial_failed`, ids/amounts only —
  CL-390). That copy survives a DSR hard-delete of the `refund_executions` row.
- DSR: `refund_executions` is in `dsr_purge._PURGE_ORDER`; the purge transaction
  sets `orchestrator.dsr_purge_in_progress` so the immutability trigger permits
  the right-to-erasure delete.
- **DSR mode is parameterized (NEEDS-FAZAL / legal):** default HARD-DELETE. Set
  `TEAM_REFUND_RETAIN_ON_DSR=1` to switch to anonymize-retain — keep
  `total_refund_paise` + `completed_at` (Indian tax/accounting may require refund
  amount+date 6-8 yrs even after a DPDP erasure) and scrub the Razorpay vendor
  detail. A config flip, not a refactor — Fazal's/legal's ruling sets it.

## Rostered follow-ups
- **VT-328 — dispatch-block:** a refunded tenant must not message customers
  (campaign dispatch) during the 30-day graceful window via ANY non-portal path.
  `portal_access_allowed()` covers portal READS; the server-side dispatch
  write-path guard belongs at the campaign-execution seam (`pre_filter_gate` has
  no phase gate today). Must derive the refunded state server-side from the
  tenant's own row (IDOR lesson). Rostered explicitly, not lost (Cowork PB2).
- **Razorpay live + per-payment refunds + the pending_subscription_cancel retry
  sweep:** VT-89.
- **Owner-notification retry** (`notification_pending`): a follow-up once Fazal
  supplies the `refund_processing` / `refund_completed` SIDs.

## Graceful exit
- `tenants.refunded_at` is set atomically with `phase=refunded` (in
  `apply_transition`). `billing/graceful_exit.portal_access_allowed(phase,
  refunded_at, now)` is the 30-day cutoff rule. The ENFORCEMENT point (team-web
  read endpoints / 403) is **VT-87** (read-only owner portal) — the dashboard is a
  scaffold today; VT-87 imports this rule so the cutoff lives in one place.
