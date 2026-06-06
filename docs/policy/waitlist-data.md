# Waitlist data — handling + erasure (VT-97)

`waitlist_signups` (migration 107) holds **pre-tenant PII** — email + WhatsApp + `consent_at` —
collected on the public landing in `waitlist` launch mode, before any tenant exists.

## Purpose limitation (CL-390)
Sole purpose: message the entrant **once**, when Viabe Team launches. `consent_at` records the
DPDP consent captured at collection (the waitlist form's mandatory consent checkbox). No other use.

## Erasure — its OWN path (NOT the tenant DSR)
`waitlist_signups` has no `tenant_id` → it is **NOT** in the tenant DSR `dsr_purge._PURGE_ORDER`
(which purges by `tenant_id`). Its erasure is a separate, explicitly-registered path so this PII
table can't silently slip the purge:

- **Explicit erasure request** — `DELETE /api/waitlist?email=<email>` (ops, X-Internal-Secret):
  hard-delete the row.
- **Post-notify purge** — after the launch sweep sets `notified_at`,
  `orchestrator.api.waitlist.purge_notified_waitlist()` hard-deletes notified rows (purpose
  fulfilled, PII no longer needed).
- **Retention bound** — `purge_stale_unnotified(months=6)` hard-deletes UN-notified rows older
  than 6 months (if launch slips), so pre-launch PII never sits unbounded.

> **Enforcement status (current):** the 6-month retention bound is **ENFORCED** (VT-354) — a
> daily DBOS scheduled job (`waitlist_retention_purge_scheduled`, 4 AM IST → `run_waitlist_
> retention_purge` → `purge_stale_unnotified(months=6)`) hard-deletes un-notified pre-launch PII
> past the bound, automatically. The HARD pre-LIVE gate on `ENABLE_WAITLIST_CAPTURE` is satisfied:
> the scheduler is live, so real waitlist PII can never sit unbounded on only a runbook promise.
> `purge_notified_waitlist()` stays an explicit ops/launch-sweep step (post-announcement). Erasure
> (`erase_waitlist`) accepts **email OR whatsapp_e164** — a principal may know only their number.

## CL-422 gate
Real waitlist PII is collected ONLY when `ENABLE_WAITLIST_CAPTURE=true` — gated on **VT-231
(Mumbai prod) + Fazal**, exactly like `ENABLE_PUBLIC_SIGNUP`. Dev (Seoul) collects ZERO real
entries: the `waitlist` mode renders the form, but the proxy 404s until the flag is set in prod.

See `docs/runbooks/launch-mode-transitions.md` for the mode-flip + launch-sweep procedure.
