-- 118_vt360_vtr_tenant_alerts_view.sql — VT-360: de-identified tenant_alerts view for the VTR.
--
-- The team-web VTR monitoring board (lib/ops/monitoring.ts) reads tenant_alerts + app-side drops
-- message_text for the VTR (maskForVtr). VT-360 routes that read through app_vtr_role + a DB view
-- instead. tenant_alerts.message_text + payload (JSONB) can carry incident free-text / identifiers,
-- so the view EXCLUDES them — explicit columns only (operational fields the VTR board shows).
-- Same model as the VT-281 views (vtr_escalations / vtr_customers): app_vtr_role gets SELECT on the
-- view ONLY; NO grant on raw tenant_alerts. REVOKE the mig-015 default-priv app_role grant for
-- tidiness (the view is the VTR surface, not an app_role surface).
-- `alert_id` is the operational ROW handle — raw UUID accepted (early-review F2: not a person
-- identifier; keyed-HMAC is for CUSTOMER refs only, of which this view has none). NO run_id
-- (early-review F3 + mig-115/117 "no run_id" rationale — a VTR holding run_id could pivot to run
-- drill-in; gate that deliberately if ever needed). tenant_name = business name (tenant-operational,
-- NOT customer PII — early-review F7; the field lives in the VIEW, not the endpoint).
CREATE OR REPLACE VIEW vtr_tenant_alerts AS
    SELECT
        a.id            AS alert_id,
        a.tenant_id,
        t.business_name AS tenant_name,
        a.trigger_kind,
        a.severity,
        a.fired_at
    FROM tenant_alerts a
    JOIN tenants t ON t.id = a.tenant_id;  -- NO message_text/payload/dedup_key/run_id

GRANT SELECT ON vtr_tenant_alerts TO app_vtr_role;
REVOKE ALL ON vtr_tenant_alerts FROM app_role;
REVOKE ALL ON vtr_tenant_alerts FROM PUBLIC;

-- MULTI-VTR precondition (same as VT-281/VT-360): Phase-1 = Fazal-as-VTR#1 sees all tenants, so
-- this view is NOT assignment-scoped. BEFORE a 2nd VTR exists it MUST be scoped to the VTR's
-- assigned tenants (a WHERE on vtr_assignments) — the orchestrator endpoint docstring carries this.
