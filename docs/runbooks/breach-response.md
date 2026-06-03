# Breach response runbook (VT-79)

> **DRAFT — pending counsel.** Same posture as `docs/policy/` drafts. The 72-hour
> DPDPA notification TEXT (en/hi) below is **placeholder-marked** and requires
> **Fazal + counsel sign-off (post-launch, VT-272-adjacent)** before any real
> notice is sent. The structure + procedures are usable now; the customer/
> authority NOTICE WORDING is not final.

## Scope
Detecting, classifying, containing, and notifying on a privacy/security breach.
Phase-1 detectors live in `orchestrator/alerts/triggers.py` (+ `pii_scrub.find_pii`)
and route through the VT-202 alert path (`dispatch_alert` → Telegram/email,
PII-scrubbed). Owner notification: `alerts/breach_notification.notify_owner`.

## Severity classification
- **P0 — confirmed cross-tenant data exposure.** Any `tenant_isolation_breach`
  (Detector-1) or `context_isolation_violation` (Detector-2, post-VT-73) that a
  post-mortem confirms reached an actual response. **72-hour DPDPA notification.**
- **P1 — detector fired, exposure likely.** A detector fired on production
  traffic; investigate within 4 hours.
- **P2 — detector fired, exposure unlikely.** Dev/test anomaly or suspected false
  positive; investigate within 24 hours.
- **P3 — information / near-miss.** e.g. PII found in logs but caught (Detector-5),
  or other near-miss; log + weekly review.

## Detectors (Phase-1 slice — VT-79)
| # | Detector | Status | Trigger kind |
|---|----------|--------|--------------|
| 1 | Tenant-isolation breach | **live** (off `tenant_isolation_breach` step) | `tenant_isolation_breach` (critical / P0) |
| 3 | DSR request-rate anomaly | **live** (fixed threshold, tune-flagged) | `dsr_rate_anomaly` (warning) |
| 5 | PII in pipeline_step payloads | **detect fn live; nightly schedule = VT-305** | `pii_in_log` (critical) |
| 2 | Context-isolation violation | DEFERRED → post-VT-73 (no source events yet) | — |
| 4 | Cross-tenant phone collision (inbound) | DEFERRED → WABA / live customer-inbound | — |
| 6 | DSR API rate-spike / IP throttle | DEFERRED → public portal (VT-231) | — |

## Response procedure (per severity)
1. **Acknowledge** — the alert lands in the ops Telegram/email (VT-202 dispatch).
2. **Contain** — for P0: identify the leak path (the `tenant_isolation_breach`
   payload carries run_id); if active, disable the offending code path / tenant.
3. **Collect evidence** — the audit chain (VT-80, immutable) + pipeline_steps are
   the record; do NOT mutate (append-only enforced).
4. **Classify** — assign P0–P3 per the table above.
5. **Notify** — see below. P0 → 72-hour DPDPA window.
6. **Post-mortem** — required for every P0 + P1 (template below).

## DPDPA notification (P0 only) — PLACEHOLDER TEXT, counsel sign-off required
> **TODO(VT-272 / counsel):** final owner-facing + customer-facing notice (en/hi).
> Owner notice is sent via `notify_owner` (interim free-form copy). Customer notice
> + authority (CERT-In) notice are DEFERRED (WABA / manual) — wording pending counsel.

- **Owner notice (interim):** see `alerts/breach_notification._OWNER_NOTICE`.
- **Customer notice:** DEFERRED (WABA + Meta template `breach_notification_customer`).
- **Authority (CERT-In):** manual — Fazal/counsel send; helper to draft the body lands
  with the final text.

## Post-mortem template (required for P0 + P1)
- Timeline (detection → containment → notification)
- Root cause
- Blast radius (tenants/customers affected; confirmed vs suspected)
- Remediation + prevention (tests/guards added)
- Notification record (who/when/what)

## Tabletop drill — SIGN-OFF (Phase-1 deliverable)
- [ ] Fazal + reviewer run a simulated P0 against this runbook before launch.
- [ ] Sign-off date: __________  (REQUIRED before treating this runbook as live.)
