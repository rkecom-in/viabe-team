-- 115_vt281_vtr_deidentified.sql — VT-281: PII unreachable from the VTR (de-identified views).
--
-- CL-426 (Fazal lock): "customer PII information encrypted so the VTR cannot see it." Fork A
-- (Cowork plan-ack 20260606T231500Z): make it DB-ENFORCED, not advisory (team-web maskForVtr was
-- app-side only). A dedicated `app_vtr_role` gets SELECT on de-identified VIEWS ONLY, and NO grant
-- on the raw PII tables (customers, phone_token_resolutions) or the decrypt function — so the role
-- physically CANNOT reach raw PII even through an app bug (the point of moving off advisory masking).
-- No at-rest encryption of name/email is added: Fork B (defends DB/service compromise) is a
-- post-launch upgrade, surfaced to Fazal; the VTR guarantee is met by no-grant.
--
-- Synthetic-only on dev (CL-422 — no real customer PII on Seoul until VT-231 Mumbai).

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- hmac() for the keyed REF#

-- 1. The VTR role. NOLOGIN (entered via SET ROLE from the service role) + NOINHERIT (it must hold
--    ONLY what is explicitly granted below — never auto-acquire privileges via a future role
--    membership). VT-271 idempotency guard (CREATE ROLE has no IF NOT EXISTS).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_vtr_role') THEN
        CREATE ROLE app_vtr_role NOLOGIN NOINHERIT;
    END IF;
END $$;
-- The service/migrator role must be a member to SET ROLE app_vtr_role (mirrors mig 015 app_role).
DO $$
BEGIN
    EXECUTE format('GRANT app_vtr_role TO %I', current_user);
END $$;

-- 2. The REF# keying secret — HMAC(customer_id, secret) (sharpening #1: KEYED, not a bare hash, so
--    the ref is not offline-linkable if ids ever leak; its job is correlation INSIDE the VTR view
--    only). Singleton row. The orchestrator seeds it from env VT_REF_HMAC_KEY
--    (orchestrator/privacy/vtr.bootstrap_vtr_ref_secret) — env, never client. The de-identified
--    views read it via the VIEW OWNER's rights (PG default security_invoker=false); app_vtr_role is
--    NEVER granted on this table, so it cannot read or reverse the secret.
CREATE TABLE IF NOT EXISTS vtr_ref_secret (
    id      BOOLEAN PRIMARY KEY DEFAULT true,   -- singleton: only one row (id = true)
    secret  TEXT NOT NULL,
    CONSTRAINT vtr_ref_secret_singleton CHECK (id)
);
-- mig 015 set ALTER DEFAULT PRIVILEGES granting app_role on new tables — REVOKE it here so the
-- secret is view-owner-only (app_role must NOT read the keying secret either). Belt: revoke PUBLIC.
REVOKE ALL ON vtr_ref_secret FROM app_role;
REVOKE ALL ON vtr_ref_secret FROM PUBLIC;
ALTER TABLE vtr_ref_secret ENABLE ROW LEVEL SECURITY;
ALTER TABLE vtr_ref_secret FORCE ROW LEVEL SECURITY;  -- deny-all; only the table owner reads it

-- 3. De-identified views (sharpening #2: explicit column lists ONLY, no `*`, no joins that could
--    smuggle a PII column). The VTR sees business/operational fields + a keyed REF#, NEVER
--    display_name / email / phone.
--
--    !!! MULTI-VTR PRECONDITION (HARD — read before adding a 2nd VTR) !!!
--    Phase-1 = Fazal is VTR#1 and sees ALL tenants, so these views are NOT tenant-scoped. BEFORE a
--    SECOND VTR exists, these views MUST be assignment-scoped — add a `vtr_assignments(vtr_id,
--    tenant_id)` table and a `WHERE tenant_id IN (SELECT tenant_id FROM vtr_assignments WHERE
--    vtr_id = <current vtr>)` to every view (the GUC-based pattern of app_current_tenant()). Without
--    that, a 2nd VTR would see every tenant's de-identified data. Do not ship multi-VTR until this
--    lands. (CL-426 multi-VTR console = VT-189, post-launch.)

CREATE OR REPLACE VIEW vtr_customers AS
    SELECT
        c.tenant_id,
        encode(hmac(c.id::text, (SELECT secret FROM vtr_ref_secret WHERE id), 'sha256'), 'hex')
            AS customer_ref,
        c.opt_out_status,
        c.source,
        c.last_inbound_at,
        c.created_at,
        c.updated_at
    FROM customers c;  -- NO display_name, NO email (the PII columns) — explicit projection only.

CREATE OR REPLACE VIEW vtr_escalations AS
    SELECT
        e.id            AS escalation_id,
        e.tenant_id,
        e.kind,
        e.severity,
        e.status,
        e.opened_at,
        e.resolved_at
    FROM escalations e;  -- NO `notes` (operator free-text could carry identity), NO run_id payload.

-- 4. Grants: USAGE on the schema + SELECT on the de-identified views ONLY. Nothing else — NO grant
--    on customers / phone_token_resolutions / resolve_phone_token_audited / vtr_ref_secret.
GRANT USAGE ON SCHEMA public TO app_vtr_role;
GRANT SELECT ON vtr_customers TO app_vtr_role;
GRANT SELECT ON vtr_escalations TO app_vtr_role;

-- 5. Grant-hygiene (sharpening #3): Postgres grants EXECUTE on every new function to PUBLIC by
--    default, so app_vtr_role would INHERIT EXECUTE on the audited decrypt fn via PUBLIC. The fn is
--    SECURITY INVOKER (its inner phone_token_resolutions read would already deny a VTR caller), but
--    defence-in-depth: REVOKE the PUBLIC EXECUTE so the VTR cannot even invoke it. app_operator_role
--    keeps its EXPLICIT grant (mig 027), so the legitimate operator decrypt path is unaffected.
REVOKE EXECUTE ON FUNCTION resolve_phone_token_audited(TEXT, TEXT) FROM PUBLIC;
