# Logfire / OTel observability outage

## Symptom

- Logfire dashboard shows zero or near-zero spans for the orchestrator
- Pipeline observability via Ops Console (`/team/ops/stream`) still works (uses Supabase Realtime, not Logfire)

## Detection

- Logfire dashboard gap
- Operator notices missing trace data for a known recent run

## Triage

1. Check Logfire service status (Pydantic-hosted)
2. Verify `LOGFIRE_TOKEN` is set in Railway env
3. Check Railway logs for `logfire:` warnings or OTel exporter errors

## Resolution

1. If Logfire service down: hold; data buffer in OTel SDK; backlog flushes on recovery
2. If token issue: rotate token in Logfire dashboard + update Railway env (Fazal authorization); restart
3. If sustained outage (> 4 hours): consider exporting OTel data to a fallback collector

## Postmortem

- Incident log
- Confirm pipeline_steps observability (the Ops Console source) was unaffected — that's the load-bearing customer-facing observability; Logfire is for debug

## Tabletop drill — last-run YYYY-MM-DD

- Outcome: NOT YET DRILLED
