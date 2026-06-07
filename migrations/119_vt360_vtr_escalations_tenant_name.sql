-- 119_vt360_vtr_escalations_tenant_name.sql — VT-360 early-review F7: add tenant_name to the view.
--
-- The team-web escalations queue shows the business name (tenant-operational, NOT customer PII). Per
-- the "a new field goes to the VIEW, never the endpoint" rule, add it to vtr_escalations rather than
-- joining in the endpoint. mig 117 (VT-280) is already merged, so this is a fresh CREATE OR REPLACE
-- (append-only: tenant_name added at the end → app_vtr_role SELECT grant preserved). `escalation_id`
-- stays the raw UUID (operational row handle for [Resolve] — early-review F2). Still NO customer PII
-- (escalations carry no customer columns); `route` is the VT-279 classifier output.
-- CREATE OR REPLACE can only APPEND columns (mig 117 fixed the first 8 + their order), so
-- tenant_name is added LAST. The endpoint SELECTs by name, so view column order doesn't matter
-- downstream.
CREATE OR REPLACE VIEW vtr_escalations AS
    SELECT
        e.id            AS escalation_id,
        e.tenant_id,
        e.kind,
        e.severity,
        e.status,
        e.opened_at,
        e.resolved_at,
        e.route,
        t.business_name AS tenant_name
    FROM escalations e
    JOIN tenants t ON t.id = e.tenant_id;  -- NO notes / run_id payload (mig 115/117 rationale)
