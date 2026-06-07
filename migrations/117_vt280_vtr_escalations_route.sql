-- 117_vt280_vtr_escalations_route.sql — VT-280: expose `route` on the VTR escalations view.
--
-- The vtr_escalations de-identified view (mig 115, VT-281) was created BEFORE escalations.route
-- (mig 116, VT-279), so it lacks the column. The VTR digest (VT-280) must show ONLY the
-- knowledge-gap items routed to the VTR (route='vtr'), so add `route` to the view. CREATE OR
-- REPLACE preserves the existing app_vtr_role SELECT grant (column appended at the end, existing
-- columns unchanged — Postgres rule). Still NO PII (route is 'vtr'|'owner'|NULL); explicit columns.
CREATE OR REPLACE VIEW vtr_escalations AS
    SELECT
        e.id            AS escalation_id,
        e.tenant_id,
        e.kind,
        e.severity,
        e.status,
        e.opened_at,
        e.resolved_at,
        e.route
    FROM escalations e;  -- NO `notes` / run_id payload; route is the VT-279 classifier output.
