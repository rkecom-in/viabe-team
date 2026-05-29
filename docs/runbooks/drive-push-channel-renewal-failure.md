# Google Drive Push channel renewal failure

## Symptom

- VT-202 alert: `drive_channel_renewal_stalled` (when wired) OR `last_notification_at > 30 min` on `tenant_drive_channels` rows that should be active
- Polling fallback (10-min cron, VT-222) covers gracefully but ingestion latency increases

## Detection

- VT-202 alert
- `SELECT count(*) FROM tenant_drive_channels WHERE expires_at < now() + interval '6 hours'` → shouldn't be > 0 if renewal scheduler is healthy
- Railway logs filtered for `drive channel renewal failed`

## Triage

1. Check Railway logs for `renew_drive_push_channel` errors
2. Verify Google Drive API quota in GCP console
3. Confirm OAuth tokens for affected tenants have `drive.metadata.readonly` scope (pre-VT-222 tenants may lack it; they fall back to polling silently)

## Resolution

1. If Drive API quota: request increase (Fazal authorization); wait for cron
2. If scope missing: opt-in re-OAuth per `docs/clau/sheet-integration-runbook.md`
3. If single channel stalled: force renew via admin endpoint `POST /api/orchestrator/admin/connector/drive_channels/<channel_id>/renew`
4. If pervasive stall: investigate the scheduler itself (see dbos-workflow-stuck.md)

## Postmortem

- Incident log
- If failure shape is new: extend `vt222_sheet_drive_push.py` canary

## Tabletop drill — last-run YYYY-MM-DD

- Outcome: NOT YET DRILLED
