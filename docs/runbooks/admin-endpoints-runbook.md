# Admin endpoints runbook (VT-224)

7 HTTP endpoints under `/api/orchestrator/admin/*`. All gated by `X-Team-Admin-Token` header matching `TEAM_ADMIN_API_TOKEN` env. In-process rate limit: 10 req/sec per token. Every call writes one row to `admin_audit_log`.

## Endpoint table

| Method | Path | Body / Query | Returns |
|---|---|---|---|
| POST | `/api/orchestrator/admin/connector/setup_push` | `{tenant_id, connector_id, spreadsheet_id?}` | `{apps_script}` (Sheet) or `{webhook_topics}` (Shopify) |
| POST | `/api/orchestrator/admin/connector/pull_sample` | `{tenant_id, connector_id, spreadsheet_id?, range?}` | `{row_count, col_count, headers}` — NO row data |
| GET  | `/api/orchestrator/admin/connector/token_shape` | `?tenant_id=&connector_id=` | `{scope_count, scopes, refresh_present, push_secret_present, *_at}` — shape only, no raw token |
| GET  | `/api/orchestrator/admin/connector/drive_channels` | `?tenant_id=` | `[]` until VT-222 ships |
| POST | `/api/orchestrator/admin/connector/drive_channels/{channel_id}/renew` | (path) | `501` until VT-222 ships |
| POST | `/api/orchestrator/admin/workflow/replay` | `{workflow_id, run_id?}` | `{replay_id, status}` |
| GET  | `/api/orchestrator/admin/health/integration_agent` | (none) | `{active_oauth_tokens, active_drive_channels, last_ingestion: [{tenant_id, connector_id, last_sync_at, last_status}]}` |

## Auth

Set `TEAM_ADMIN_API_TOKEN` in Railway env. Recommended: 32-byte hex.

```bash
openssl rand -hex 32
# → drop into Railway env as TEAM_ADMIN_API_TOKEN
```

Endpoint returns `503` if env is unset, `403` on missing/wrong header.

## curl examples

### Token shape

```bash
ORCH=https://vt-orchestrator-service-development.up.railway.app
TOKEN=...                # your TEAM_ADMIN_API_TOKEN
TENANT=8e5fa032-bd81-4af6-b3bb-30a0ad47e00b

curl -H "X-Team-Admin-Token: $TOKEN" \
  "$ORCH/api/orchestrator/admin/connector/token_shape?tenant_id=$TENANT&connector_id=google_sheet"
```

### Setup push (Google Sheet)

```bash
curl -X POST \
  -H "X-Team-Admin-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"'"$TENANT"'","connector_id":"google_sheet","spreadsheet_id":"1DaOh580..."}' \
  "$ORCH/api/orchestrator/admin/connector/setup_push"
```

### Pull sample

```bash
curl -X POST \
  -H "X-Team-Admin-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"'"$TENANT"'","connector_id":"google_sheet","spreadsheet_id":"1DaOh580...","range":"Sheet1!A1:Z50"}' \
  "$ORCH/api/orchestrator/admin/connector/pull_sample"
```

Returns shape only; never returns row contents.

### Integration agent health

```bash
curl -H "X-Team-Admin-Token: $TOKEN" \
  "$ORCH/api/orchestrator/admin/health/integration_agent"
```

### Workflow replay

```bash
curl -X POST \
  -H "X-Team-Admin-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"workflow_id":"<dbos-workflow-id>"}' \
  "$ORCH/api/orchestrator/admin/workflow/replay"
```

## Audit log

Every call writes one row. Inspect via:

```sql
-- Recent admin activity
SELECT invoked_at, endpoint, tenant_id, response_status, token_fingerprint, error_message
FROM admin_audit_log
ORDER BY invoked_at DESC
LIMIT 50;

-- Activity for a specific tenant
SELECT invoked_at, endpoint, response_status, error_message
FROM admin_audit_log
WHERE tenant_id = '8e5fa032-bd81-4af6-b3bb-30a0ad47e00b'
ORDER BY invoked_at DESC
LIMIT 50;

-- Calls by a specific admin token (use the fingerprint from the audit log)
SELECT invoked_at, endpoint, tenant_id, response_status
FROM admin_audit_log
WHERE token_fingerprint = '<8-char-fingerprint>'
ORDER BY invoked_at DESC;
```

`token_fingerprint` is the first 8 chars of `sha256(token)` — never the raw token. Compute it locally if you need to filter by your own token:

```bash
echo -n "$TOKEN" | shasum -a 256 | cut -c1-8
```

## Token rotation

1. Generate a new token: `openssl rand -hex 32`
2. Update Railway env `TEAM_ADMIN_API_TOKEN` with the new value
3. Restart the orchestrator service
4. Distribute the new token to admins via the secrets channel
5. Old token's fingerprint stops appearing in `admin_audit_log` post-rotation; pre-rotation audit lines remain traceable by the old fingerprint

## Rate limit

10 requests/sec per admin token. Burst over → 429 with `Retry-After` semantics implicit (caller back off ~1s). Phase-1 in-process counter; multi-orchestrator deploys need a Redis-backed bucket — file VT-22N when ready.

## Privacy locks (CL-390 cluster + VT-224 review)

- `token_shape` returns SHAPE only — never raw token values
- `pull_sample` returns row_count + col_count + column headers (schema, OK to expose); NO row data, even scrubbed
- Audit log stores `token_fingerprint` (8-char sha256 prefix); never raw token

## Future work

- Web UI for admin operations (Phase 2)
- Bulk operations (per-tenant only in Phase 1)
- Connector listing endpoint
- Multi-admin role separation (single shared token in Phase 1)
- Redis-backed rate limit for multi-orchestrator deploys
- Deployment-status webhook → admin audit forward (file when needed)
