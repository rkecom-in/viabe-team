-- 027_operator_role_jwt_substrate.sql — VT-188 operator-role JWT substrate.
--
-- VT-122.10 — operator-role substrate for VT-123 Ops UI client-direct
-- read path. Per Fazal decision (2026-05-26): VT-123 Ops UI requires a
-- client-direct read path (in addition to backend reads) so admin
-- console can perform low-latency PII unmask without round-tripping
-- through orchestrator backend.
--
-- CL-88: client-direct JWT pattern (Supabase auth + PostgREST + RLS).
-- CL-82: extends existing 4 RLS policies on phone_token_resolutions
--        with a 5th operator-role variation.
-- CL-417: no canonical column changes (VT-187 already shipped).
-- CL-416: no row deletion in operator path.
-- CL-390 / CL-330: per-resolution audit log mandatory.
-- CL-150: privacy_audit_log 7-year retention (migration 008).
--
-- Note on AFTER-SELECT trigger: Postgres does NOT support SELECT
-- triggers. Per Q3 Option A (Cowork plan-review locked): atomicity
-- enforced at app-layer via the `resolve_phone_token_audited()`
-- stored function below (SELECT + audit INSERT in single transaction;
-- raises if audit fails → rollback).

-- Single migration per Q2 Option B: role + helper + policy + stored
-- function. Can split into 000c_operator_helpers.sql later if more
-- operator-specific helpers accumulate.

CREATE ROLE app_operator_role NOLOGIN INHERIT;

GRANT USAGE ON SCHEMA public TO app_operator_role;
GRANT EXECUTE ON FUNCTION app_current_tenant() TO app_operator_role;
GRANT SELECT ON phone_token_resolutions TO app_operator_role;
GRANT INSERT ON privacy_audit_log TO app_operator_role;


-- Helper: operator-claim presence check.
-- Reads the JWT-derived GUC `app.jwt.operator_claim` (set by
-- PostgREST/Supabase from the request JWT's app_metadata.role claim).
-- Phase 1 — claim presence is sufficient; the iat/exp + audit-session
-- window enforcement is application-layer (route validates JWT
-- expiration before dispatching to RPC).
CREATE OR REPLACE FUNCTION app_operator_audit_enabled() RETURNS boolean
    LANGUAGE plpgsql STABLE AS $$
DECLARE
    claim TEXT;
BEGIN
    claim := current_setting('app.jwt.operator_claim', true);
    IF claim IS NULL OR claim = '' THEN
        RETURN false;
    END IF;
    RETURN true;
END;
$$;

GRANT EXECUTE ON FUNCTION app_operator_audit_enabled() TO app_operator_role;


-- Operator SELECT policy variation on phone_token_resolutions.
-- Existing 4 policies (select/insert/update/delete on app_role —
-- though app_role has no GRANT here per VT-178 BY-GRANT-EXCLUSION)
-- remain untouched. This adds a 5th policy specifically for
-- app_operator_role SELECTs.
-- AS RESTRICTIVE so this policy ANDs with the existing PERMISSIVE
-- phone_token_resolutions_select policy (migration 007) instead of
-- OR-ing. Without RESTRICTIVE, the union of PERMISSIVE policies makes
-- the operator policy redundant for grant-holders — operator could
-- SELECT under the base tenant policy even without an operator claim.
-- TO app_operator_role scopes the RESTRICTIVE to operator-role
-- connections only; app_role and the service-role path are unaffected.
CREATE POLICY phone_token_resolutions_operator_select ON phone_token_resolutions
    AS RESTRICTIVE
    FOR SELECT TO app_operator_role
    USING (
        tenant_id = app_current_tenant()
        AND app_operator_audit_enabled()
    );


-- Stored function — atomic resolve + audit per Q6 Option A.
-- The operator-role connection invokes this via Supabase RPC. The
-- function runs SELECT + audit INSERT inside a single transaction; if
-- the audit INSERT fails (RLS denies, constraint violation, etc.),
-- the SELECT result is never returned (transaction rollback).
-- Atomicity at the DB layer.
CREATE OR REPLACE FUNCTION resolve_phone_token_audited(
    p_phone_token TEXT,
    p_operator_id TEXT
) RETURNS TEXT
    LANGUAGE plpgsql AS $$
DECLARE
    v_phone TEXT;
    v_tenant UUID;
BEGIN
    -- SELECT scoped by current GUC tenant + operator-claim policy.
    -- RLS enforces tenant_id = app_current_tenant() AND operator-claim
    -- present. Result is NULL when policy denies.
    SELECT phone_number_encrypted, tenant_id
      INTO v_phone, v_tenant
      FROM phone_token_resolutions
     WHERE phone_token = p_phone_token;

    -- Audit row. tenant_id from the resolved row (NULL if SELECT
    -- denied → NULL violates privacy_audit_log RLS WITH CHECK).
    -- Resolution-without-audit is impossible: either both succeed or
    -- both roll back.
    INSERT INTO privacy_audit_log (
        tenant_id, event_type, payload, this_hash, actor
    ) VALUES (
        v_tenant,
        'phone_token_resolved',
        jsonb_build_object(
            'phone_token', p_phone_token,
            'resolved', v_phone IS NOT NULL,
            'operator_id', p_operator_id,
            'via_jwt', true
        ),
        encode(
            sha256(concat(p_operator_id, ':', p_phone_token)::bytea),
            'hex'
        ),
        p_operator_id
    );

    RETURN v_phone;
END;
$$;

GRANT EXECUTE ON FUNCTION resolve_phone_token_audited(TEXT, TEXT)
    TO app_operator_role;


-- Grant app_operator_role membership to the role running this migration
-- so `SET ROLE app_operator_role` works at runtime. Mirrors migration
-- 015_app_role.sql line 44 pattern: CI runs as `postgres`, Supabase /
-- production runs as the secret-key role — both need membership.
DO $$
BEGIN
    EXECUTE format('GRANT app_operator_role TO %I', current_user);
END $$;

-- Defense-in-depth audit column comment (parallels VT-184 / migration 026
-- pattern): make the column's plaintext-until-VT-191 warning visible
-- here too, so anyone inspecting the operator-substrate also sees the
-- pre-prod gate. Idempotent — re-declares the comment from 026.
COMMENT ON COLUMN phone_token_resolutions.phone_number_encrypted IS
    'PLAINTEXT until VT-191 encryption — DO NOT promote to prod without encryption per CL-390 privacy posture';
