-- 031_tenant_integration_state.sql — VT-206 Integration Agent state substrate.
--
-- One row per tenant. Tracks onboarding progress through 5 phases. The
-- Integration Agent reads this on every invocation to resume mid-flow
-- after disconnects (brief AC-3).
--
-- Per CL-19 typed envelopes — `pending_owner_input` JSONB carries
-- serialized Pydantic ``PendingOwnerInput`` model from
-- ``agent/integration_agent.py`` (per Cowork Q2 flag: validate writes
-- through the Pydantic model, even though storage is JSONB).
-- Per CL-26 — workspace-level memory; cohort-keyed L0 fragments
-- (future) capture which connectors succeed for which business
-- archetypes.
-- Per CL-71 — tenant-scoped RLS.
-- Per CL-416 — no delete path; DSR-purge owns deletion.

CREATE TABLE IF NOT EXISTS public.tenant_integration_state (
    tenant_id              UUID PRIMARY KEY REFERENCES tenants(id),
    phase                  TEXT NOT NULL DEFAULT 'phase_1_discovery'
                           CHECK (phase IN (
                               'phase_1_discovery',
                               'phase_2_auth',
                               'phase_3_sample_pull',
                               'phase_4_field_mapping',
                               'phase_5_confirmed'
                           )),
    current_connector_id   TEXT,
    pending_owner_input    JSONB,
    last_decision          JSONB,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tenant_integration_phase
    ON public.tenant_integration_state (phase);

ALTER TABLE public.tenant_integration_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_integration_state FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_integration_state_select ON public.tenant_integration_state;
CREATE POLICY tenant_integration_state_select ON public.tenant_integration_state
    FOR SELECT USING (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_integration_state_insert ON public.tenant_integration_state;
CREATE POLICY tenant_integration_state_insert ON public.tenant_integration_state
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_integration_state_update ON public.tenant_integration_state;
CREATE POLICY tenant_integration_state_update ON public.tenant_integration_state
    FOR UPDATE USING (tenant_id = app_current_tenant())
                WITH CHECK (tenant_id = app_current_tenant());

DROP POLICY IF EXISTS tenant_integration_state_delete ON public.tenant_integration_state;
CREATE POLICY tenant_integration_state_delete ON public.tenant_integration_state
    FOR DELETE USING (tenant_id = app_current_tenant());

-- Operator-claim SELECT (mirrors migration 030 pattern). Phase-1
-- Fazal-only operator surface; Phase-2 multi-operator review.
DROP POLICY IF EXISTS tenant_integration_state_operator_select
    ON public.tenant_integration_state;
CREATE POLICY tenant_integration_state_operator_select
    ON public.tenant_integration_state
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );

COMMENT ON TABLE public.tenant_integration_state IS
    'VT-206 Integration Agent per-tenant onboarding state. One row per tenant. pending_owner_input stores PendingOwnerInput (Pydantic) serialized as JSONB.';
