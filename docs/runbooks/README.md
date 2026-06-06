# Ops runbooks

> **📖 Canonical document** (VT-119)
>
> Authoritative for: **operational procedures** (production incident response).
> NOT authoritative for: architectural decisions (→ `docs/adr/`) or current architecture (→ `docs/Viabe_Team_Technical_Reference_v1_0.md`).
> Source-of-truth hierarchy: **parallel** to the Technical Reference (procedures, not architecture).
> Update protocol: one runbook per incident class (per `0000-template.md`); each drills ≥ once before the Reports-Jun15 launch.
> Last reviewed: 2026-06-06 · Next review: pre-launch drill pass.

Operator-facing runbooks for production incidents. Each file follows the template (`0000-template.md`). Tabletop drill cadence: each runbook must drill ≥ once before Reports-Jun15 launch milestone.

## Index

| Runbook | Scenario | Last drill |
|---|---|---|
| [twilio-webhook-outage.md](twilio-webhook-outage.md) | Twilio inbound webhook 5xx / no deliveries | NOT YET DRILLED |
| [supabase-region-failover.md](supabase-region-failover.md) | Supabase region down | NOT YET DRILLED |
| [dbos-workflow-stuck.md](dbos-workflow-stuck.md) | DBOS scheduled workflow not firing / stuck | NOT YET DRILLED |
| [anthropic-rate-limit.md](anthropic-rate-limit.md) | Anthropic API 429 surge | NOT YET DRILLED |
| [razorpay-webhook-signature-mismatch.md](razorpay-webhook-signature-mismatch.md) | Razorpay payment webhook signature failure | NOT YET DRILLED |
| [apify-actor-failure.md](apify-actor-failure.md) | Apify ingestion actor failure | NOT YET DRILLED |
| [drive-push-channel-renewal-failure.md](drive-push-channel-renewal-failure.md) | Google Drive Push channel renewal fails | NOT YET DRILLED |
| [logfire-observability-outage.md](logfire-observability-outage.md) | Logfire / OTel pipeline down | NOT YET DRILLED |
| [dsr-export-workflow.md](dsr-export-workflow.md) | DPDP data-subject-request export | NOT YET DRILLED |
| [pii-redaction-failure.md](pii-redaction-failure.md) | PII redact helper missing / regression | NOT YET DRILLED |
| [operator-jwt-compromise.md](operator-jwt-compromise.md) | OPERATOR_JWT_SECRET leaked | NOT YET DRILLED |

## Related clau-internal runbooks

- `docs/clau/admin-endpoints-runbook.md` (VT-224) — admin endpoints reference
- `docs/clau/sheet-integration-runbook.md` (VT-222) — Sheet connector lifecycle
- `docs/clau/dev-env-runbook.md` — dev environment Railway/Vercel
- `docs/clau/region-verify-runbook.md` (VT-169) — Supabase region residency
