-- 130_vt370_vtr_console_views.sql — VT-370 Gap-6: the VTR console's DB-enforced read surface.
--
-- Rule: DB view > app code. app_vtr_role has ZERO grants on the raw business_plan /
-- agent_draft_batches / agent_drafts / tenant_agent_autonomy tables — these views are the only
-- door (the mig-115 construction: owner-rights views, NOT security_invoker, deliberately bypassing
-- RLS with the role grant as the sole gate).
--
-- NOTE on mig 124's "deliberately NO view": that comment declined a view on the app_role/OWNER
-- read path to avoid a security_invoker RLS hole. These vtr_* views are the OPPOSITE construction
-- (owner-rights + role-gated) — the established mig-115 pattern; mig 124's decision stands for
-- app_role (the owner path keeps ORDER BY version DESC LIMIT 1 directly).
--
-- MULTI-VTR PRECONDITION (same as mig 115/118): these views are NOT assignment-scoped. Before a
-- 2nd (non-Fazal) VTR: add WHERE tenant_id IN (SELECT ... FROM vtr_assignments) to ALL, and reopen
-- the Devanagari proper-noun validator gap (VT-368/VT-370 pre-promotion list).
--
-- EXCLUDED BY CONSTRUCTION (CL-390 — the PII boundary):
--   business_plan:        fact_bundle_json (fact values), ALL prior versions, diff_from_prev VALUES
--                         (the redaction paradox: an edited-out name must not persist on the VTR
--                         surface; field-name KEYS survive — they show WHAT changed, never to-what)
--   agent_draft_batches:  owner_feedback (owner free text)
--   agent_drafts:         params (customer names by design), customer_id, message_sid
--   tenant_agent_autonomy: revoke_reason (free text; PR-3 writes owner-originated text into it)

-- The exception-tier role (Fazal=VTR#1): may read agent_drafts param-level detail through the
-- dedicated audited endpoint ONLY. Mirrors mig 115's idempotent CREATE ROLE guard.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_vtr_admin_role') THEN
        CREATE ROLE app_vtr_admin_role NOLOGIN NOINHERIT;
    END IF;
END $$;
DO $$
BEGIN
    EXECUTE format('GRANT app_vtr_admin_role TO %I', current_user);
END $$;

-- Latest version ONLY; diff_from_prev VALUES stripped (keys kept).
CREATE OR REPLACE VIEW vtr_business_plan AS
SELECT p.tenant_id, p.version, p.summary_json,
       (SELECT jsonb_agg(
            CASE WHEN item #> '{provenance,diff_from_prev}' IS NULL THEN item
                 ELSE jsonb_set(item, '{provenance,diff_from_prev}',
                      COALESCE((SELECT jsonb_agg(k) FROM jsonb_object_keys(
                          item #> '{provenance,diff_from_prev}') AS k), '[]'::jsonb), false)
            END ORDER BY (item->>'seq')::int)
        FROM jsonb_array_elements(p.roadmap_json) AS item)        AS roadmap_json,
       p.generated_by, p.model_id, p.delivered_parts, p.delivered_at, p.created_at
FROM (SELECT DISTINCT ON (tenant_id) * FROM business_plan
      ORDER BY tenant_id, version DESC) p;

CREATE OR REPLACE VIEW vtr_plan_history AS
    SELECT tenant_id, version, generated_by, model_id, created_at FROM business_plan;

CREATE OR REPLACE VIEW vtr_agent_autonomy AS
    SELECT a.tenant_id, t.business_name AS tenant_name, a.agent, a.level,
           a.clean_approval_streak, a.lifetime_approvals, a.lifetime_rejections, a.frozen,
           a.last_regression_at, a.last_regression_kind,
           a.l3_granted_at, a.l3_revoked_at, a.updated_at
    FROM tenant_agent_autonomy a JOIN tenants t ON t.id = a.tenant_id;

CREATE OR REPLACE VIEW vtr_draft_batches AS
    SELECT b.id AS batch_id, b.tenant_id, t.business_name AS tenant_name, b.agent,
           b.status, b.edit_cycles, b.created_at, b.updated_at,
           count(d.id)                                     AS draft_count,
           count(d.id) FILTER (WHERE d.status = 'drafted') AS pending_count,
           count(d.id) FILTER (WHERE d.status = 'sent')    AS sent_count,
           count(d.id) FILTER (WHERE d.status = 'skipped') AS skipped_count,
           count(d.id) FILTER (WHERE d.status = 'halted')  AS halted_count,
           array_agg(DISTINCT d.template_name)             AS template_names
    FROM agent_draft_batches b
    JOIN tenants t ON t.id = b.tenant_id
    LEFT JOIN agent_drafts d ON d.tenant_id = b.tenant_id AND d.batch_id = b.id
    GROUP BY b.id, b.tenant_id, t.business_name, b.agent, b.status, b.edit_cycles,
             b.created_at, b.updated_at;

-- The exception-tier param-level view (the audited Fazal drill-in reads through this; the
-- standard app_vtr_role has NO grant on it).
CREATE OR REPLACE VIEW vtr_admin_batch_drafts AS
    SELECT d.tenant_id, d.batch_id, d.id AS draft_id, d.template_name, d.params,
           d.status, d.skip_reason, d.created_at
    FROM agent_drafts d;

GRANT SELECT ON vtr_business_plan  TO app_vtr_role;
GRANT SELECT ON vtr_plan_history   TO app_vtr_role;
GRANT SELECT ON vtr_agent_autonomy TO app_vtr_role;
GRANT SELECT ON vtr_draft_batches  TO app_vtr_role;
GRANT USAGE ON SCHEMA public TO app_vtr_admin_role;
GRANT SELECT ON vtr_admin_batch_drafts TO app_vtr_admin_role;
GRANT SELECT ON vtr_business_plan, vtr_plan_history, vtr_agent_autonomy, vtr_draft_batches
    TO app_vtr_admin_role;
