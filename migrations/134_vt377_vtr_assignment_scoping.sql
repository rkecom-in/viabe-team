-- 134_vt377_vtr_assignment_scoping.sql — VT-377 Phase D: assignment-scope the NINE app_vtr_role
-- views (the CL-426 second-VTR precondition) + the VT-381 F2 REVOKE rider.
--
-- THIS MIGRATION CLOSES THE MULTI-VTR PRECONDITION carried in the mig-115/118/130/131/132
-- docstrings, and RETIRES their stale `vtr_assignments` references: the assignment substrate is
-- the EXISTING `operator_assignments` table (mig-072) — the same table `require_vtr_action`
-- already gates on — NOT a new `vtr_assignments` table (plan-ack amendment, Cowork-approved
-- 20260612T011000Z; forking the substrate would be divergence-by-construction, the N1 lesson).
--
-- Mechanism (Cowork ruling, all three arms approved):
--   * `app_vtr_operator()` — ONE helper mirroring 000b's `app_current_tenant()`: reads the
--     `app.vtr_operator_id` GUC (set txn-local by `privacy/vtr.vtr_connection(operator_id=...)`
--     post-JWT-verify; NEVER from a client field). THE RULING'S PREDICATE FIX: a bare
--     `current_setting(..., true)::uuid` THROWS on an empty-string GUC (missing_ok returns ''
--     if the GUC was ever set non-locally on a pooled session — ''::uuid is a cast error that
--     would 500 every view query instead of failing closed). NULLIF makes unset OR empty ⇒ NULL
--     ⇒ the scoped subquery matches nothing — fail-closed, never a cast error.
--   * Every app_vtr_role view gains ONE appended predicate:
--       current_user = 'app_vtr_admin_role' OR tenant_id IN (SELECT tenant_id FROM
--       operator_assignments WHERE operator_id = app_vtr_operator() AND unassigned_at IS NULL)
--     A VTR sees ONLY tenants with an ACTIVE assignment (unassigned_at IS NULL — revoked rows
--     scope to nothing). The ADMIN tier (Fazal=VTR#1, `SET ROLE app_vtr_admin_role`) keeps
--     all-tenants via the role leg — role IS the mechanism, no bypass flags (ruling-approved).
--     The subquery reads operator_assignments via the VIEW OWNER's rights (owner-rights views,
--     mig-115 construction; the owner/service role is RLS-bypassing) — app_vtr_role itself still
--     has ZERO grant on operator_assignments.
--   * GRANT SELECT on all nine to app_vtr_admin_role (existing role, mig-130) so the admin tier
--     can actually enter the role leg on every view (it previously held only the console four).
--
-- DEFENSE IN DEPTH, not a replacement: `require_vtr_action`'s per-tenant assignment gate and the
-- endpoints' single-tenant WHERE clauses all STAY — this predicate is the DB-enforced floor
-- under them (an app bug can no longer leak a foreign tenant's de-identified rows to a VTR).
--
-- View bodies are copied VERBATIM from each view's LATEST defining migration — vtr_customers
-- (115), vtr_escalations (119 — 117 added route, 119 added tenant_name), vtr_tenant_alerts (118),
-- vtr_business_plan / vtr_plan_history / vtr_agent_autonomy / vtr_draft_batches (130),
-- vtr_workflow_controls (131), vtr_step_timeline (132 — the C2 key-projection body) — with ONLY
-- the predicate appended (before GROUP BY where one exists). CREATE OR REPLACE preserves the
-- existing app_vtr_role grants (same column lists — a REPLACE requirement).
-- vtr_admin_batch_drafts (mig-130 exception-tier drill-in) is deliberately NOT touched: it is
-- not an app_vtr_role view; its door stays the audited SET LOCAL ROLE endpoint.

-- ─── 1. The operator helper (the ruling's predicate fix, ONE place) ───

-- Mirrors 000b app_current_tenant(): unset OR empty GUC ⇒ NULL ⇒ scoped subquery matches
-- nothing — fail-closed, never a cast error.
CREATE OR REPLACE FUNCTION app_vtr_operator() RETURNS uuid
    LANGUAGE sql
    STABLE
AS $$
    SELECT NULLIF(current_setting('app.vtr_operator_id', true), '')::uuid
$$;

-- ─── 2. The nine app_vtr_role views, assignment-scoped ───

-- Body: mig-115 verbatim + the predicate.
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
    FROM customers c  -- NO display_name, NO email (the PII columns) — explicit projection only.
    WHERE current_user = 'app_vtr_admin_role'
       OR c.tenant_id IN (SELECT tenant_id FROM operator_assignments
                          WHERE operator_id = app_vtr_operator() AND unassigned_at IS NULL);

-- Body: mig-119 verbatim (the CURRENT vtr_escalations — 8 mig-117 columns + tenant_name) + the
-- predicate. NOTE: the build contract's "115/118 originals" list missed migs 117/119; copying
-- 115's 7-column body here would have broken ops_resolve (it SELECTs route + tenant_name).
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
    JOIN tenants t ON t.id = e.tenant_id  -- NO notes / run_id payload (mig 115/117 rationale)
    WHERE current_user = 'app_vtr_admin_role'
       OR e.tenant_id IN (SELECT tenant_id FROM operator_assignments
                          WHERE operator_id = app_vtr_operator() AND unassigned_at IS NULL);

-- Body: mig-118 verbatim + the predicate.
CREATE OR REPLACE VIEW vtr_tenant_alerts AS
    SELECT
        a.id            AS alert_id,
        a.tenant_id,
        t.business_name AS tenant_name,
        a.trigger_kind,
        a.severity,
        a.fired_at
    FROM tenant_alerts a
    JOIN tenants t ON t.id = a.tenant_id  -- NO message_text/payload/dedup_key/run_id
    WHERE current_user = 'app_vtr_admin_role'
       OR a.tenant_id IN (SELECT tenant_id FROM operator_assignments
                          WHERE operator_id = app_vtr_operator() AND unassigned_at IS NULL);

-- Body: mig-130 verbatim (latest version ONLY; diff_from_prev VALUES stripped, keys kept) + the
-- predicate on the outer (DISTINCT ON) projection.
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
      ORDER BY tenant_id, version DESC) p
WHERE current_user = 'app_vtr_admin_role'
   OR p.tenant_id IN (SELECT tenant_id FROM operator_assignments
                      WHERE operator_id = app_vtr_operator() AND unassigned_at IS NULL);

-- Body: mig-130 verbatim + the predicate.
CREATE OR REPLACE VIEW vtr_plan_history AS
    SELECT tenant_id, version, generated_by, model_id, created_at FROM business_plan
    WHERE current_user = 'app_vtr_admin_role'
       OR tenant_id IN (SELECT tenant_id FROM operator_assignments
                        WHERE operator_id = app_vtr_operator() AND unassigned_at IS NULL);

-- Body: mig-130 verbatim + the predicate.
CREATE OR REPLACE VIEW vtr_agent_autonomy AS
    SELECT a.tenant_id, t.business_name AS tenant_name, a.agent, a.level,
           a.clean_approval_streak, a.lifetime_approvals, a.lifetime_rejections, a.frozen,
           a.last_regression_at, a.last_regression_kind,
           a.l3_granted_at, a.l3_revoked_at, a.updated_at
    FROM tenant_agent_autonomy a JOIN tenants t ON t.id = a.tenant_id
    WHERE current_user = 'app_vtr_admin_role'
       OR a.tenant_id IN (SELECT tenant_id FROM operator_assignments
                          WHERE operator_id = app_vtr_operator() AND unassigned_at IS NULL);

-- Body: mig-130 verbatim + the predicate (a WHERE precedes the GROUP BY — row filter, not a
-- post-aggregate HAVING, so an unassigned tenant's batches never even enter the aggregate).
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
    WHERE current_user = 'app_vtr_admin_role'
       OR b.tenant_id IN (SELECT tenant_id FROM operator_assignments
                          WHERE operator_id = app_vtr_operator() AND unassigned_at IS NULL)
    GROUP BY b.id, b.tenant_id, t.business_name, b.agent, b.status, b.edit_cycles,
             b.created_at, b.updated_at;

-- Body: mig-132 verbatim (the CURRENT vtr_step_timeline — the VT-376 C2 explicit key
-- projections; mig-131's was superseded) + the predicate on r.tenant_id (qualified —
-- pipeline_steps carries tenant_id too).
CREATE OR REPLACE VIEW vtr_step_timeline AS
SELECT r.tenant_id,
       r.id         AS run_id,
       r.run_type,
       r.status     AS run_status,
       r.started_at AS run_started_at,
       r.ended_at   AS run_ended_at,
       r.rerun_of_run_id,
       r.rerun_from_step,
       s.id         AS step_id,
       s.step_seq,
       s.step_kind,
       s.step_name,
       s.status     AS step_status,
       s.started_at,
       s.ended_at,
       s.duration_ms,
       s.override_id,
       s.paused_ms,
       CASE WHEN s.step_kind = 'webhook_received'
                     AND jsonb_typeof(s.input_envelope) = 'object'
                -- mig-131 4-key allowlist, unchanged: body_token / sender_phone_token /
                -- twilio_message_sid / media_url_0 are NEVER exposed.
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'message_type',          s.input_envelope -> 'message_type',
                         'num_media',             s.input_envelope -> 'num_media',
                         'dupe_status',           s.input_envelope -> 'dupe_status',
                         'status_callback_state', s.input_envelope -> 'status_callback_state'))
            WHEN s.step_kind = 'agent_invocation'
                     AND jsonb_typeof(s.input_envelope) = 'object'
                -- VT-376 C2: explicit writer-key projection (was whole-envelope).
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'inbound_body_len', s.input_envelope -> 'inbound_body_len',
                         'trigger',          s.input_envelope -> 'trigger',
                         'dispatched_at',    s.input_envelope -> 'dispatched_at'))
            WHEN s.step_kind = 'aborted_hard_limit'
                     AND jsonb_typeof(s.input_envelope) = 'object'
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'reason',           s.input_envelope -> 'reason',
                         'inbound_body_len', s.input_envelope -> 'inbound_body_len'))
            WHEN s.step_kind = 'tenant_isolation_breach'
                     AND jsonb_typeof(s.input_envelope) = 'object'
                -- The writer never sets input for this kind; allowlisted anyway (see header).
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'layer',           s.input_envelope -> 'layer',
                         'offending_ids',   s.input_envelope -> 'offending_ids',
                         'counts',          s.input_envelope -> 'counts',
                         'expected_tenant', s.input_envelope -> 'expected_tenant',
                         'stray_tenants',   s.input_envelope -> 'stray_tenants'))
            WHEN s.input_envelope IS NULL THEN NULL
            WHEN jsonb_typeof(s.input_envelope) = 'object'
                THEN COALESCE((SELECT jsonb_agg(k)
                               FROM jsonb_object_keys(s.input_envelope) AS k), '[]'::jsonb)
            ELSE '[]'::jsonb
       END AS input_envelope,
       CASE WHEN s.step_kind = 'webhook_received'
                     AND jsonb_typeof(s.output_envelope) = 'object'
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'message_type',          s.output_envelope -> 'message_type',
                         'num_media',             s.output_envelope -> 'num_media',
                         'dupe_status',           s.output_envelope -> 'dupe_status',
                         'status_callback_state', s.output_envelope -> 'status_callback_state'))
            WHEN s.step_kind = 'agent_invocation'
                     AND jsonb_typeof(s.output_envelope) = 'object'
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'reason', s.output_envelope -> 'reason'))
            WHEN s.step_kind = 'aborted_hard_limit'
                     AND jsonb_typeof(s.output_envelope) = 'object'
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'axis',     s.output_envelope -> 'axis',
                         'observed', s.output_envelope -> 'observed',
                         'limit',    s.output_envelope -> 'limit'))
            WHEN s.step_kind = 'tenant_isolation_breach'
                     AND jsonb_typeof(s.output_envelope) = 'object'
                -- BOTH _record_breach shapes project: pre-flight (layer/offending_ids/
                -- counts) and post-flight (layer/expected_tenant/stray_tenants).
                THEN jsonb_strip_nulls(jsonb_build_object(
                         'layer',           s.output_envelope -> 'layer',
                         'offending_ids',   s.output_envelope -> 'offending_ids',
                         'counts',          s.output_envelope -> 'counts',
                         'expected_tenant', s.output_envelope -> 'expected_tenant',
                         'stray_tenants',   s.output_envelope -> 'stray_tenants'))
            WHEN s.output_envelope IS NULL THEN NULL
            WHEN jsonb_typeof(s.output_envelope) = 'object'
                THEN COALESCE((SELECT jsonb_agg(k)
                               FROM jsonb_object_keys(s.output_envelope) AS k), '[]'::jsonb)
            ELSE '[]'::jsonb
       END AS output_envelope
FROM pipeline_runs r
LEFT JOIN pipeline_steps s ON s.run_id = r.id
WHERE current_user = 'app_vtr_admin_role'
   OR r.tenant_id IN (SELECT tenant_id FROM operator_assignments
                      WHERE operator_id = app_vtr_operator() AND unassigned_at IS NULL);

-- Body: mig-131 verbatim + the predicate.
CREATE OR REPLACE VIEW vtr_workflow_controls AS
    SELECT tenant_id, workflow_kind, set_at, released_at FROM workflow_controls
    WHERE current_user = 'app_vtr_admin_role'
       OR tenant_id IN (SELECT tenant_id FROM operator_assignments
                        WHERE operator_id = app_vtr_operator() AND unassigned_at IS NULL);

-- ─── 3. Admin-tier grants: the role leg needs SELECT on every view it opens ───

-- app_vtr_admin_role previously held only the console four (mig-130) + vtr_admin_batch_drafts.
-- The all-tenants tier (Fazal=VTR#1: digest, break-glass reads) now needs all nine.
GRANT SELECT ON vtr_customers, vtr_escalations, vtr_tenant_alerts, vtr_business_plan,
    vtr_plan_history, vtr_agent_autonomy, vtr_draft_batches, vtr_step_timeline,
    vtr_workflow_controls
    TO app_vtr_admin_role;

-- ─── 4. VT-381 F2 rider — ops_top_tenants_today anon/authenticated REVOKEs ───

-- mig-133 revoked PUBLIC only; on Supabase the platform defaults grant EXECUTE to
-- anon/authenticated DIRECTLY, and a direct grant survives a PUBLIC revoke (the VT-376 gate
-- finding — RLS saved it, defense-in-depth gap only). Existence-guarded: the roles exist on
-- Supabase only (local/CI Postgres applies clean — the mig-130/133 guard pattern).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE EXECUTE ON FUNCTION ops_top_tenants_today(integer, timestamptz) FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE EXECUTE ON FUNCTION ops_top_tenants_today(integer, timestamptz) FROM authenticated;
    END IF;
END $$;

-- MULTI-VTR PRECONDITION note mig-133 lacked (VT-381 F2): ops_top_tenants_today is a
-- service_role-only function and is NOT assignment-scoped at the function level — it lists the
-- whole fleet by construction (the Ops Console fleet ranking). VTR scoping for the panel is
-- CALLER-SIDE: team-web intersects the function's rows with the operator's active assignments
-- (resolveAssignedTenants) before rendering tenant tiles; admin sees all. Approved as the VT-377
-- mechanism (Cowork ruling 20260612T011000Z) and covered by the panel-leg test.
