-- 124_vt368_business_plan.sql — VT-368 Gap-4: the business-summary + 6-month-roadmap SPINE.
--
-- One append-only row per (tenant, version): the owner's proactive business summary + the ORDERED
-- roadmap Gap-5 specialist agents execute against and Gap-6 VTR edits (each edit = a NEW version,
-- non-destructive; the table IS the audit log). fact_bundle_json freezes the grounding facts THIS
-- version cited — readers re-verify citations offline; Gap-6 edits re-ground against the SAME frozen
-- facts (no silent KG drift). Content is immutable post-insert; only delivered_parts/delivered_at
-- (delivery metadata) update in place. Latest plan = ORDER BY version DESC LIMIT 1 (deliberately NO
-- view — avoids any security_invoker RLS hole).
--
-- Tenant business data → RLS + FORCE in this migration (the table is also touched by the privileged
-- owner pool via dsr_purge) AND swept in dsr_purge._PURGE_ORDER (hard-delete leaf — the VT-323/366
-- lesson: a new tenant table forgotten in the purge order survives DSR).

CREATE TABLE business_plan (
    tenant_id        UUID NOT NULL REFERENCES tenants (id),
    version          INTEGER NOT NULL,                    -- 1..N dense per tenant; latest = max(version)
    summary_json     JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {text, text_hi, cited_facts, headline_metrics}
    roadmap_json     JSONB NOT NULL DEFAULT '[]'::jsonb,  -- ordered item array (stable item_id per item)
    fact_bundle_json JSONB NOT NULL DEFAULT '{}'::jsonb,  -- frozen {Fid:{key,value,source}} grounding facts
    generated_by     TEXT NOT NULL,                       -- 'gap4_generator' | 'vtr:<id>' | <agent_name>
    model_id         TEXT,                                -- resolved LLM model id (audit; NULL for edits)
    delivered_parts  INTEGER NOT NULL DEFAULT 0,          -- bitmap of sent parts (idempotent replay resume)
    delivered_at     TIMESTAMPTZ,                         -- stamped after the FINAL part
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, version)
);
CREATE INDEX business_plan_latest ON business_plan (tenant_id, version DESC);

-- Pillar 3: RLS in the SAME migration. FORCE so ownership alone cannot bypass (mirrors mig 122/123).
ALTER TABLE business_plan ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_plan FORCE ROW LEVEL SECURITY;
CREATE POLICY business_plan_select ON business_plan FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY business_plan_insert ON business_plan FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY business_plan_update ON business_plan FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY business_plan_delete ON business_plan FOR DELETE
    USING (tenant_id = app_current_tenant());
