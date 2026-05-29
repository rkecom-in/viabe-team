# ADR-0008: Operator-JWT vs admin-token auth split

**Status:** Accepted

## Context

Two distinct auth concerns:

1. **Operator UI access** — Ops Console pages (`/team/ops/*`) viewed by Fazal (and Phase-2 operators). Needs session lifecycle, browser cookie semantics, magic-link or email/password sign-in via Supabase Auth, JWT carrying operator-role claim for Supabase Realtime + RLS.
2. **Admin endpoint access** — server-to-server `POST /api/orchestrator/admin/*` calls from CLI / curl / scripts. Needs a long-lived API token, rate limiting, per-call audit log. No browser session involved.

Conflating these (single API key for both, or single JWT for both) creates problems: API keys can't refresh; JWTs don't suit shell scripts; rate-limit policies differ.

## Considered Options

- **A.** Single operator-JWT everywhere — server-to-server scripts have to mint short-lived JWTs each time; high friction
- **B.** Single API token everywhere — no browser session model; can't drive RLS policies that need per-user claims
- **C.** Split: operator-JWT for UI; admin token for server-to-server (chosen)

## Decision

**C.** Two parallel auth substrates:

- **operator-JWT** (HS256, OPERATOR_JWT_SECRET) issued via `/api/ops/login` (VT-203); 1-hour TTL; HttpOnly Secure cookie scoped to `/team/ops/*` (ADR-0006). Used by `requireFazal()` to gate Ops Console pages + by Supabase Realtime client to derive operator-claim for cross-tenant RLS reads. Magic-link sign-in via Supabase Auth.
- **admin-token** (`TEAM_ADMIN_API_TOKEN` env, 32-byte hex) verified via `X-Team-Admin-Token` header on `/api/orchestrator/admin/*` (VT-224). In-process rate limit 10 req/sec per token. Every call writes one row to `admin_audit_log` with 8-char sha256 fingerprint (never raw token). Rotation: regenerate, update Railway env, restart service.

## Consequences

- (+) Clear surface separation — Ops Console code never sees admin token; CLI scripts never need browser cookies
- (+) Independent rotation cadence (JWT secret rotates on operator key compromise; admin token rotates on operator turnover)
- (+) Audit log only tracks server-to-server calls — Ops UI clicks are already in `pipeline_steps`
- (+) operator-JWT can drive Supabase RLS (HS256 claims); admin token bypasses RLS via service role (admin operations are intentionally cross-tenant)
- (−) Two secrets to manage (acceptable; both have rotation procedures documented in runbooks)
- (−) Magic-link flow needs Supabase Auth project configured (VT-203 dependency)
- (−) Admin endpoints need explicit per-call audit code (boilerplate; centralised in `_auth.py::log_admin_call`)

## References

- CL-220 (operator-JWT for Ops UI)
- VT-188 (operator JWT issue/refresh)
- VT-203 (Ops Console login surface)
- VT-224 (admin endpoints suite)
- docs/clau/admin-endpoints-runbook.md
- ADR-0006 (cookie scoping by path)
