# Dev environment runbook (VT-218)

## What this is

The dev environment is the public-URL deploy of both Viabe Team apps used for integration testing against real-world surfaces (Apps Script, Shopify webhooks, Resend, Telegram). Two deploys, one Postgres:

| Surface | Host | URL |
|---|---|---|
| **orchestrator** | Railway Pro | `https://<railway-domain>.up.railway.app` |
| **team-web** | Vercel Pro | `https://<vercel-domain>.vercel.app` |
| **database** | Supabase dev | (see `.viabe/secrets/supabase-dev.env`) |

Push to `main` → both deploy automatically via `.github/workflows/deploy-dev.yml`.

## First-time setup (Fazal-side, one-time)

### Railway

1. Create new Railway project linked to `rkecom-in/viabe-team` GitHub repo
2. Add a service from the repo root (Dockerfile path: `apps/team-orchestrator/Dockerfile`)
3. Settings → Networking → Public Networking → Generate Domain
4. Copy the Railway-provided URL → use as `RAILWAY_DEV_URL` GitHub secret
5. Copy the service ID (from Railway URL or service settings) → `RAILWAY_SERVICE_ID` GitHub secret
6. Copy your Railway API token (Account → Tokens → New) → `RAILWAY_TOKEN` GitHub secret
7. Populate env vars in Railway dashboard (see "Railway env-var matrix" below)

### Vercel

1. Create new Vercel project linked to the same repo
2. Root directory: `apps/team-web`
3. Framework: Next.js (auto-detected from `vercel.json`)
4. Copy the Vercel-provided URL → use as `VERCEL_DEV_URL` GitHub secret
5. Copy Vercel personal access token → `VERCEL_TOKEN` GitHub secret
6. Project settings → General → Project ID → `VERCEL_PROJECT_ID` GitHub secret
7. Team settings → Org ID → `VERCEL_ORG_ID` GitHub secret
8. Populate env vars in Vercel dashboard (see "Vercel env-var matrix" below)

### GitHub repo secrets summary

Settings → Secrets and variables → Actions → New repository secret:

- `RAILWAY_TOKEN`
- `RAILWAY_SERVICE_ID`
- `RAILWAY_DEV_URL`
- `VERCEL_TOKEN`
- `VERCEL_ORG_ID`
- `VERCEL_PROJECT_ID`
- `VERCEL_DEV_URL`

### Google OAuth client update

Add Railway URL to GCP OAuth client's Authorized redirect URIs:
```
https://<railway-domain>.up.railway.app/api/orchestrator/integrations/google/callback
```
Keep the localhost entry too for local dev.

### Supabase Realtime

Verify `pipeline_steps` table has Realtime enabled (Database → Replication in Supabase Studio). VT-201 PR-1 may have already enabled it.

## Railway env-var matrix (set in Railway dashboard)

Source these from `.viabe/secrets/*.env` files. Never commit values.

| Var | Source file | Notes |
|---|---|---|
| `DATABASE_URL` | supabase-dev.env | direct Postgres DSN, NOT REST URL |
| `OPERATOR_JWT_SECRET` | supabase-dev.env | HS256; 32+ bytes |
| `INTERNAL_API_SECRET` | supabase-dev.env | shared with team-web |
| `TEAM_PHONE_ENCRYPTION_KEY` | supabase-dev.env | Fernet key |
| `TEAM_PHONE_HASH_SALT` | supabase-dev.env | 16+ bytes |
| `SUPABASE_URL` | supabase-dev.env | REST URL (different from DATABASE_URL) |
| `SUPABASE_SECRET_KEY` | supabase-dev.env | server-only Supabase service-role-equivalent |
| `FAZAL_OWNER_UUID` | supabase-dev.env | operator UUID |
| `FAZAL_TENANT_ID` | supabase-dev.env | RKeCom tenants row UUID |
| `GOOGLE_OAUTH_CLIENT_ID` | google-oauth.env | |
| `GOOGLE_OAUTH_CLIENT_SECRET` | google-oauth.env | |
| `GOOGLE_OAUTH_REDIRECT_URI` | google-oauth.env | **MUST** be the Railway URL, not localhost |
| `TELEGRAM_DEV_BOT_TOKEN` | telegram.env | |
| `TELEGRAM_DEV_CHAT_ID` | telegram.env | |
| `TELEGRAM_OPS_BOT_TOKEN` | telegram.env | |
| `TELEGRAM_OPS_CHAT_ID` | telegram.env | |
| `RESEND_API_KEY` | resend.env | |
| `RESEND_FROM_EMAIL` | resend.env | verified sender |
| `RESEND_TO_EMAIL` | resend.env | Fazal's inbox |
| `ANTHROPIC_API_KEY` | anthropic.env | `sk-ant-` prefix |
| `TEAM_TWILIO_ACCOUNT_SID` | (not yet provisioned) | optional; set when Twilio dev creds ready |
| `TEAM_TWILIO_AUTH_TOKEN` | (not yet provisioned) | optional |
| `TEAM_CANARY_TENANT_IDS` | (none) | leave unset for dev; tests set per-canary |

## Vercel env-var matrix (set in Vercel dashboard)

Server-only by default unless `NEXT_PUBLIC_` prefix.

| Var | Source | Notes |
|---|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | supabase-dev.env | client-side Supabase URL |
| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | supabase-dev.env | client-side RLS-safe key |
| `NEXT_PUBLIC_SITE_URL` | (your Vercel URL) | for `lib/auth/require-fazal.ts` cookie domain |
| `OPERATOR_JWT_SECRET` | supabase-dev.env | server-side; same as Railway |
| `INTERNAL_API_SECRET` | supabase-dev.env | same as Railway |
| `FAZAL_OWNER_UUID` | supabase-dev.env | same as Railway |
| `FAZAL_TENANT_ID` | supabase-dev.env | same as Railway |
| `TEAM_ORCHESTRATOR_URL` | (Railway dev URL) | server-side; team-web → orchestrator |
| `SUPABASE_URL` | supabase-dev.env | server-only (used by `lib/supabase-client.ts:serverSecretClient`) |
| `SUPABASE_SECRET_KEY` | supabase-dev.env | server-only |

## How to apply a hot-fix

Vercel preview deploys land for every feature branch automatically. The Railway service only deploys on push to `main`, so for a hot-fix that needs orchestrator changes:

1. Open PR with the fix
2. Verify Vercel preview URL behaves correctly (web changes only)
3. Once PR merges to main, Railway picks it up and deploys

For team-web-only hot-fixes, the preview URL is enough to test before merging.

## Rollback

### Vercel

Project → Deployments → click the previous successful deploy → "Promote to Production".

### Railway

```
railway rollback --service <RAILWAY_SERVICE_ID>
```
or via dashboard: Service → Deployments → click prior deploy → "Redeploy this version".

## Secrets rotation

Both Vercel + Railway support env-var rotation without a code deploy:

1. Update the value in the respective dashboard
2. Trigger a redeploy (Vercel: "Redeploy" button; Railway: `railway redeploy`)

For secrets stored in both apps simultaneously (`OPERATOR_JWT_SECRET`, `INTERNAL_API_SECRET`, etc.), update both dashboards then redeploy both — otherwise auth breaks until the slower deploy catches up.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Railway deploy 502 | Migration ran but uvicorn crashed | Check Railway logs for `migrations applied` then traceback |
| Vercel build fails on `pnpm typecheck` | env-var dependent type | Add the env to Vercel project env list, mark "Build" scope |
| `/health` 200 but stream 500 | Supabase env mismatch | Verify SUPABASE_URL + SECRET_KEY in Railway match supabase-dev.env |
| Apps Script fails to POST | Old localhost URL in script | Regenerate Apps Script via `setup_push` after Railway URL takes effect |
| OAuth callback 404 | GCP redirect URI not updated | Add Railway URL to GCP OAuth client's Authorized redirect URIs |

## Smoke test (run manually any time)

```
export RAILWAY_DEV_URL=https://<railway-domain>.up.railway.app
export VERCEL_DEV_URL=https://<vercel-domain>.vercel.app
bash scripts/dev-env-smoke.sh
```

Exits 0 on full pass; non-zero (with which assertion failed) on any red.

## Prod environment

Out of scope for VT-218. Filed as separate row when ready to promote. Same shape (Railway + Vercel + Supabase prod project) but separate URLs, separate secrets, separate Resend/Telegram bots.

## Preview Deploys (VT-223)

### Vercel dashboard toggle

To enable PR-side preview deploys:

1. Vercel dashboard → `viabe-team` project → Settings → Git
2. Locate the "Pull Request Previews" toggle (exact label may vary by Vercel version)
3. Enable for the `viabe-team` GitHub repository
4. Verify by pushing a feature branch with a touched `apps/team-web/` file — preview check should appear within ~2 min on the PR

### `vercel.json` `github.silent`

Set to `false` (per VT-223). When `true`, Vercel suppresses commit-status checks on PRs even if previews fire — debugging deploy issues becomes opaque. The trade-off is more PR-noise; we accept that vs flying blind.

### `vercel.json` `ignoreCommand`

`git diff --quiet HEAD^ HEAD ./` — skips Vercel build when the PR diff doesn't touch `apps/team-web/`. Kept (per VT-223) for build-cost optimization.

To force a deploy on a docs-only PR (e.g., to test identity propagation, rebuild env, etc.), include any 1-line edit under `apps/team-web/` — e.g., append a blank line to `apps/team-web/README.md`. VT-221 used this pattern.

### Sticky-deploy recovery

If a Vercel deploy is stuck in-progress (>10 min for what should be a 2-min build):

1. Vercel dashboard → Deployments → click the stuck deploy → "Cancel"
2. Open the latest commit on `main` → "Redeploy"
3. Verify status reaches `Ready` within ~3 min

For diagnosis, see `.viabe/queue/VT-223/diagnostic.md`. Most-likely cause from the 2026-05-28 incident: webhook race during a force-push leaving Vercel with an orphan deploy reference. Avoid force-pushes during active deploys when possible.

### Future work — deployment-status webhooks

To get telemetry on stuck deploys, enable Vercel deployment-status webhooks (`deployment.error` / `deployment.canceled` / `deployment.created` events) and forward into the VT-202 alert substrate. Filed as note in `.viabe/queue/VT-223/diagnostic.md`. Allocate a VT row when ready to wire it.
