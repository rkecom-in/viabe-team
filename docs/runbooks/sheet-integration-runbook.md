# Sheet integration runbook (VT-222 / CL-421)

Drive Push Notifications primary + 10-minute polling fallback. Zero manual paste after OAuth.

## Owner flow

1. Owner clicks "Connect Google Sheet" on `/team/onboard`
2. Redirected to Google OAuth consent (scopes: `spreadsheets.readonly` + `drive.metadata.readonly`)
3. Owner grants access
4. Callback completes → orchestrator persists `tenant_oauth_tokens` row
5. Orchestrator auto-registers a Drive Push channel for the chosen Sheet
6. Owner is done — no Apps Script paste, no triggers to add

## Substrate

- **Push channel**: `tenant_drive_channels` table tracks active channels. One row per (tenant, sheet). 7-day TTL (Google's max). Renewal scheduler runs every 6h, renews channels expiring within 48h.
- **Webhook**: `POST /api/orchestrator/integrations/sheet/drive_push` receives Drive notifications. Verifies `X-Goog-Channel-Token` against the stored row. Enqueues `pull_sheet_delta_workflow` on `update` state.
- **Polling fallback**: `@DBOS.scheduled("*/10 * * * *")` `poll_unwatched_sheets_body` picks up tenants with NO active channel OR `last_notification_at` older than 30 min.

## Scope migration (existing tenants)

Tenants onboarded BEFORE VT-222 may have only the `spreadsheets.readonly` scope. Drive Push registration will fail for them with a Google API "insufficient scope" error. They will fall back to polling-only (every 10 min) silently.

To upgrade an existing tenant to Drive Push:

1. Operator (or integration agent) prompts the owner: "Your Sheet connection works via 10-minute polling. For real-time updates, I can ask you to re-grant access — would you like that?"
2. Owner opts in
3. Orchestrator triggers the OAuth flow again — Google's consent screen shows the new scope (`drive.metadata.readonly`)
4. Owner grants
5. Callback persists the upgraded scope → next push registration succeeds

This is opt-in, NOT forced. Polling-only tenants are not degraded users — their data still lands within 10 minutes.

## Operator interventions

### Inspect active channels

```bash
curl -H "X-Team-Admin-Token: $TOKEN" \
  "$ORCH/api/orchestrator/admin/connector/drive_channels?tenant_id=<tenant-uuid>"
```

Returns array of `{channel_id, resource_id, expires_at, created_at, last_notification_at}`.

### Force renew a channel

```bash
curl -X POST -H "X-Team-Admin-Token: $TOKEN" \
  "$ORCH/api/orchestrator/admin/connector/drive_channels/<channel_id>/renew"
```

Registers a new channel, returns the new descriptor, deletes the old row.

### Stuck channel

If a channel exists but `last_notification_at` keeps going stale (>30 min), polling will kick in to bridge the gap. To force a fresh push channel, call renew above; if that also stalls, drop the row via SQL and let polling cover until the next OAuth-time re-register:

```sql
DELETE FROM tenant_drive_channels WHERE channel_id = '<id>';
```

## Channel lifecycle

```
register_drive_push_channel
        │
        ▼
   POST drive/v3/files/<id>/watch
        │ 200
        ▼
INSERT tenant_drive_channels (channel_id, channel_token, expires_at + 7d)
        │
        ▼  (Google fires notifications on file change)
POST /api/orchestrator/integrations/sheet/drive_push
        │ verify channel_token (hmac.compare_digest)
        ▼
UPDATE last_notification_at = now()
DBOS.start_workflow(pull_sheet_delta_workflow, ...)
        │
        ▼
pull_full → field-mapping (VT-209) → dedupe (VT-184) → INSERT into customers tagged acquired_via='google_sheet'

renew_expiring_drive_channels_body (every 6h)
        │ finds channels expiring within 48h
        ▼
register_drive_push_channel (new) → INSERT new row
unregister_drive_push_channel (old) → DELETE old row

poll_unwatched_sheets_body (every 10 min)
        │ finds tenants with no channel OR last_notification_at > 30min
        ▼
DBOS.start_workflow(pull_sheet_delta_workflow, ...)
```

## Apps Script deprecation

`setup_push` + `apps_script_template.render_apps_script` are deprecated per CL-421. Kept for backward compat with tenants that had Apps Script registered pre-VT-222 (their `sheet_push` webhook still fires). Do NOT extend; do NOT reference from new prompts or onboarding flows. New Sheet connector flows use Drive Push only.

## Privacy

- `channel_token` is a verify-only secret stored in plaintext. Not a credential. Used only for inbound webhook validation.
- Webhook handler returns `401` BEFORE any DB write on token mismatch.
- No raw OAuth tokens logged. Token refresh + Drive API calls reuse existing `get_access_token` substrate.
- Sheet row contents follow the existing PII pipeline (phone-hash via VT-184; field-mapping via VT-209). Drive Push is transport-layer only.

## Failure modes + handling

| Failure | Detect | Recover |
|---|---|---|
| OAuth scope too narrow (pre-VT-222 tenants) | Drive watch returns 403 / insufficient scope | Polling fallback runs every 10 min; opt-in re-OAuth grants the new scope |
| Drive API rate limit | Drive watch returns 429 | Renewal scheduler retries on the next 6h tick; webhook polling fallback still works |
| Webhook delivery delayed >30 min | `last_notification_at` stale | Polling scheduler picks up the delta on its next 10-min tick |
| Channel expired before renewal | `expires_at < now` row found by polling worker | Polling pulls rows; renewal scheduler will re-register on next tick |
| `ORCHESTRATOR_BASE_URL` env unset | Drive watch returns 400 (invalid address) | Set env in Railway dashboard; restart service |

## Future work

- Multi-Sheet per tenant (Phase 2)
- Per-tenant real-time vs polling preference toggle (Phase 2)
- Non-Sheet Drive file types
- Apps Script substrate migration tool for existing tenants (auto-call re-OAuth flow)
