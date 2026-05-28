"""VT-202 — proactive alerts substrate.

Push-complement to VT-201's pull dashboard. Detects anomalies +
threshold breaches in pipeline_runs / pipeline_steps / privacy_audit_log
and fires Telegram + Resend email notifications.

Architecture (Cowork-locked 2026-05-28):
- httpx.AsyncClient direct against Telegram Bot API + Resend API; no SDKs
- Pure SQL window functions for baselines (NOT in-process aggregation)
- Write-then-dispatch: insert tenant_alerts row first, then fire;
  HTTP failure leaves row for next scheduler tick retry
- PII scrub: load-bearing dispatch step; phone digits / SIDs stripped
- Canary tenant routing via TEAM_CANARY_TENANT_IDS env (NOT name match)

Public surface:
- ``dispatch_alert(...)`` — write-then-dispatch entry; called from both
  the runner.py write-hook (criticals) and the 5-min scheduler (slow).
- ``register_alert_scheduler()`` — applied in main.py lifespan
  before launch_dbos. Same register-before-launch contract as VT-210.
- ``is_canary_tenant(tenant_id)`` — env-gated whitelist check.
"""
