# DBOS workflow stuck

## Symptom

- Scheduled workflow not firing on expected cadence (e.g., 5-min ingestion poller silent for > 15 min)
- `workflow_status` table shows rows in `PENDING` state past expected completion time
- Railway logs: `DBOSWorkflowFunctionNotFoundError` recurring (was the VT-215 root cause)

## Detection

- VT-202 alert: `dbos_scheduler_silent`
- Railway log tail filtered for `dbos:` warnings
- `SELECT count(*) FROM dbos.workflow_status WHERE status='PENDING' AND created_at < now() - interval '15 min'`

## Triage

1. Check Railway logs for `DBOSWorkflowFunctionNotFoundError` → if present, a `@DBOS.workflow()` decoration is missing from a scheduled body (VT-215 pattern)
2. Check `dbos.workflow_status` for stuck rows + their input payloads
3. Check Supabase reachability — DBOS depends on Postgres availability
4. Check service start-time vs schedule — if the service restarted mid-cron, the next tick may be delayed up to one cron interval

## Resolution

1. If `WorkflowFunctionNotFoundError`: ship a fix mirroring VT-215 (`DBOS.workflow()(fn)` before `DBOS.scheduled(cron)(fn)`); deploy; restart
2. If stuck row: `DBOS.resume_workflow(workflow_id)` via the admin endpoint `POST /api/orchestrator/admin/workflow/replay` (VT-224)
3. If Postgres reachability issue: see supabase-region-failover.md
4. If start-time mismatch: trigger the workflow manually via admin endpoint

## Postmortem

- Incident log
- Confirm vt200_hygiene_bundle canary covers the affected scheduler (if not, extend per VT-215 pattern)

## Tabletop drill — last-run YYYY-MM-DD

- Outcome: NOT YET DRILLED
