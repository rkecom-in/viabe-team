-- 046_operator_allowlist.sql — VT-228 dynamic operator allowlist.
--
-- Replaces the hardcoded FAZAL_OWNER_UUID operator check (VT-203/233/237)
-- with a DB-backed allowlist so Phase-2 multi-operator (magic-link grants
-- for Cowork/support) works without code changes. team-web reads it at
-- the auth callsites; the orchestrator admin API grants/revokes.
--
-- VT-228 reconciliations (Cowork review 2026-05-30):
-- - NO FK to auth.users: the CI migrations job runs plain pg16 with no
--   Supabase `auth` schema (VT-170 precedent — FK'd tenants, not
--   auth.users). user_id is a bare UUID PK; the app validates it.
-- - Workspace-scoped (no tenant_id). FORCE RLS with NO policies =
--   deny-all: only the service-role connection (team-web serverSecretClient
--   / orchestrator pool, which bypass RLS) can touch it. Defense-in-depth
--   for an auth table.
-- - Seed: NONE in this migration. Fazal is covered by the FAZAL_OWNER_UUID
--   break-glass in app code (lib/auth/operator-allowlist.ts), so the table
--   starts empty and grants are added via the admin endpoint. No
--   auth.users subquery (unqueryable in the migration runner).
-- CL-422: any data is synthetic on dev until prod-in-Mumbai (VT-231).

CREATE TABLE IF NOT EXISTS public.operator_allowlist (
    user_id     UUID PRIMARY KEY,              -- Supabase Auth user UUID (no FK; auth schema is separate)
    granted_by  UUID NULL,                     -- operator who granted (NULL for the initial seed)
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ NULL,              -- non-NULL = revoked (kept for audit, not deleted)
    revoke_reason TEXT NULL,
    notes       TEXT NULL
);

-- Active-operator lookup path (revoked_at IS NULL) — the hot query from
-- the per-request require-fazal check.
CREATE INDEX IF NOT EXISTS idx_operator_allowlist_active
    ON public.operator_allowlist (user_id)
    WHERE revoked_at IS NULL;

-- Deny-all RLS: no policies + FORCE → only service-role (RLS-bypassing)
-- connections see/modify rows. anon/tenant roles get nothing.
ALTER TABLE public.operator_allowlist ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.operator_allowlist FORCE ROW LEVEL SECURITY;
