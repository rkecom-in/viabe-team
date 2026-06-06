# Owner messaging budget + approval defer (VT-334)

## Per-week messaging budget
Max **2** `campaign_send` approval requests per owner per **7 days** (`_WEEKLY_APPROVAL_BUDGET`
in `collapse.py`). `collapse_node` (reached via the weekly-cadence fan-out) counts the tenant's
`campaign_send` approvals in the last 7 days (`PendingApprovalsWrapper.count_recent_campaign_requests`,
indexed by `(tenant_id, created_at)`); at the cap it **skips sending a new approval prompt**.

The proposed campaign is still persisted (`campaigns` row, status `proposed`) — the owner sees it
next sync; only the *prompt* is withheld (owner-fatigue guard). **Silent by design** — a
"we didn't notify you" message would defeat the guard. An `approval_budget_skipped` `log_event`
records the skip for observability (no PII).

## Approval "defer"
An owner reply of *later / baad mein / बाद में / अगले हफ़्ते / next week* classifies as `defer`
(deterministic — `owner_inputs/approval_reply.py`; precedence **reject > defer > approve**, so any
negation still wins as a reject).

A defer **EXTENDS** the approval window 48h: `pending_approvals.defer_count += 1`,
`timeout_at += 48h`, the row stays `status='pending'` (`resolved_at` NULL — the run stays paused).
After `_MAX_DEFERS` (**2**) the next defer is treated as a **rejection**: `decision='defer'`,
`status='rejected'` — the safe downstream behavior (consumers read `status` and don't send), with
the audit truth in `decision='defer'`.

**Known limitation (accepted, fail-safe — Cowork 20260606T103500Z):** no negation handling for
defer keywords — *"send now instead of later"* (containing "later") classifies as `defer`. This is
the fail-safe direction: the campaign is delayed and the owner is re-asked, never an unconsented
send. VT-329 may add full negation treatment for defer.
