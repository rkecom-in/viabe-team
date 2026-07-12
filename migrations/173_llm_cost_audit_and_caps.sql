-- 173_llm_cost_audit_and_caps.sql — Fazal directive 2026-07-13 (blocking, pre-resume):
-- FULL per-tenant LLM cost audit + VTR-controlled caps, across BOTH providers
-- (Anthropic sonnet-5 / opus-4.8 / haiku-4.5 + OpenAI gpt-5.6-sol / -terra / -luna).
--
--   * ``model_pricing``     — GLOBAL price registry (USD per MTok in/out, per model), VTR-writable.
--                             Seeded with placeholders where public pricing is unconfirmed —
--                             every seed row is Fazal/VTR-tunable via the ops console; costing
--                             math never hard-codes a price.
--   * ``llm_call_events``   — the PER-CALL audit ledger (Fazal: "each LLM call must be recorded"):
--                             tenant, agent, call_site, model, service_tier, tokens in/out, and the
--                             COMPUTED cost_usd at the ledger's own recorded price. Tenant-scoped
--                             RLS + FORCE (same class as tm_audit). tenant_id NULLABLE: platform
--                             calls with no tenant (blind judges, plan validators run tenantless)
--                             are still recorded (audit completeness) under the service role.
--   * ``tenant_llm_limits`` — PER-TENANT caps (Fazal: "cap model usage on tenant"), VTR-ADMIN-ONLY
--                             writes via the ops console (the API layer enforces the admin gate —
--                             the same _gate pattern as vtr-plan). The DB-level protection that the
--                             runtime can ENFORCE but never SELF-EDIT is FORCE RLS + a SELECT-only
--                             policy (NO UPDATE/INSERT policy): app_role carries a legacy blanket
--                             table-level UPDATE grant from an earlier default-privileges migration,
--                             so the row-level policy — not the grant — is the real guard (an
--                             app_role UPDATE hits 0 rows; verified empirically, same model as
--                             agent_cost_limits/171).
--   * ``global_llm_limits`` — the OVERALL platform cap (Fazal: "and overall"), singleton row,
--                             VTR-admin-only, same read-only-to-runtime posture.
--
-- Enforcement reads llm_call_events aggregates (+ the VT-619 tenant_agent_usage counters where
-- present); soft threshold -> notify once per period; hard -> degrade to the deterministic nets
-- with an honest owner message. Enforcement NEVER bends the money gates.

CREATE TABLE model_pricing (
    model             TEXT PRIMARY KEY,
    usd_per_mtok_in   NUMERIC(10, 4) NOT NULL,
    usd_per_mtok_out  NUMERIC(10, 4) NOT NULL,
    -- Cache-read price as a fraction of input (BOTH providers price cache hits at 0.1x input:
    -- Anthropic "Cache Hits & Refreshes", OpenAI "cached input").
    cached_in_multiplier NUMERIC(4, 3) NOT NULL DEFAULT 0.1,
    -- Discounted-tier multiplier applied when service_tier IN ('flex','batch') — verified
    -- 2026-07-13 on BOTH providers: OpenAI Flex == Batch == 50%; Anthropic Batches API == 50%
    -- (both input AND output).
    discount_multiplier NUMERIC(4, 3) NOT NULL DEFAULT 0.5,
    updated_by        TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE model_pricing ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_pricing FORCE ROW LEVEL SECURITY;
CREATE POLICY model_pricing_select ON model_pricing FOR SELECT USING (true);
GRANT SELECT ON model_pricing TO app_role;
-- writes: service role only (VTR console runs server-side with the service connection).

-- Seed — VERIFIED 2026-07-13 against platform.claude.com/docs pricing + developers.openai.com
-- pricing (Fazal-provided links). NOTE: claude-sonnet-5 is INTRODUCTORY $2/$10 through
-- 2026-08-31, then $3/$15 from 2026-09-01 — VTR updates the row on Sep 1 (the console endpoint
-- exists for exactly this).
INSERT INTO model_pricing (model, usd_per_mtok_in, usd_per_mtok_out, updated_by) VALUES
    ('claude-sonnet-5',   2.0000, 10.0000, 'seed-173-intro-until-2026-08-31'),
    ('claude-opus-4-8',   5.0000, 25.0000, 'seed-173'),
    ('claude-haiku-4-5-20251001', 1.0000, 5.0000, 'seed-173'),
    ('claude-haiku-4-5',  1.0000,  5.0000, 'seed-173'),
    ('gpt-5.6-sol',       5.0000, 30.0000, 'seed-173'),
    ('gpt-5.6-terra',     2.5000, 15.0000, 'seed-173'),
    ('gpt-5.6-luna',      1.0000,  6.0000, 'seed-173');

CREATE TABLE llm_call_events (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     UUID REFERENCES tenants (id) ON DELETE CASCADE,   -- NULL = platform/tenantless call
    agent         TEXT NOT NULL,          -- team_manager / sales_recovery_agent / triage / judge / …
    call_site     TEXT NOT NULL,          -- dispatch_brain / triage_checkpoint / onboarding_brain / …
    provider      TEXT NOT NULL,          -- anthropic | openai
    model         TEXT NOT NULL,
    service_tier  TEXT NOT NULL DEFAULT 'standard',   -- standard | flex
    tokens_in     BIGINT NOT NULL DEFAULT 0,
    tokens_out    BIGINT NOT NULL DEFAULT 0,
    cost_usd      NUMERIC(12, 6) NOT NULL DEFAULT 0,  -- computed AT WRITE from model_pricing
    request_id    TEXT,                                -- provider request id when available
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX llm_call_events_tenant_time ON llm_call_events (tenant_id, occurred_at DESC);
CREATE INDEX llm_call_events_time ON llm_call_events (occurred_at DESC);

ALTER TABLE llm_call_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_call_events FORCE ROW LEVEL SECURITY;
-- INSERT+SELECT for the tenant's own rows; the recorder INSERTs under the tenant GUC. Platform
-- (NULL-tenant) rows are service-role territory — invisible to app_role by policy.
CREATE POLICY llm_call_events_tenant ON llm_call_events FOR ALL
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT ON llm_call_events TO app_role;

CREATE TABLE tenant_llm_limits (
    tenant_id            UUID PRIMARY KEY REFERENCES tenants (id) ON DELETE CASCADE,
    max_cost_usd_month   NUMERIC(10, 2),      -- NULL = no per-tenant cost cap
    max_tokens_in_month  BIGINT,              -- NULL = no token cap
    max_tokens_out_month BIGINT,
    soft_pct             INT NOT NULL DEFAULT 80,
    enabled              BOOLEAN NOT NULL DEFAULT true,
    set_by               TEXT NOT NULL,       -- VTR admin identity (the console records who)
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE tenant_llm_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_llm_limits FORCE ROW LEVEL SECURITY;
-- Runtime may READ its own tenant's limits (to enforce); ONLY the VTR console (service role)
-- writes — app_role deliberately gets no INSERT/UPDATE (Fazal: "Only VTR admin can set").
CREATE POLICY tenant_llm_limits_read ON tenant_llm_limits FOR SELECT
    USING (tenant_id = app_current_tenant());
GRANT SELECT ON tenant_llm_limits TO app_role;

CREATE TABLE global_llm_limits (
    id                  BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),   -- singleton row
    max_cost_usd_day    NUMERIC(12, 2),
    max_cost_usd_month  NUMERIC(12, 2),
    soft_pct            INT NOT NULL DEFAULT 80,
    enabled             BOOLEAN NOT NULL DEFAULT true,
    set_by              TEXT NOT NULL DEFAULT 'seed-173',
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE global_llm_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE global_llm_limits FORCE ROW LEVEL SECURITY;
CREATE POLICY global_llm_limits_read ON global_llm_limits FOR SELECT USING (true);
GRANT SELECT ON global_llm_limits TO app_role;

-- Seed the singleton with caps DISABLED-by-value (NULL caps = record-only until Fazal/VTR sets
-- real numbers via the console; the enforcement layer treats NULL as "no cap").
INSERT INTO global_llm_limits (id, max_cost_usd_day, max_cost_usd_month) VALUES (true, NULL, NULL);
