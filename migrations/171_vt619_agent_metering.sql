-- 171_vt619_agent_metering.sql — VT-619: per-tenant × per-agent LLM token/API-call METERING
-- + config-driven soft/hard LIMITS.
--
-- Two tables with DELIBERATELY different scoping:
--
--   * ``agent_cost_limits`` — GLOBAL config (the product's cap envelope per agent). Not
--     tenant-scoped: the SAME caps apply to every tenant (a POLICY placeholder Fazal tunes).
--     RLS + FORCE with a permissive ``FOR SELECT USING (true)`` so any tenant connection may READ
--     its caps; NO write policy, so app_role writes are RLS-denied — only the BYPASSRLS service
--     role seeds/tunes caps. ``enabled=false`` = TRACKED-but-UNBILLED (setup agents: integration /
--     onboarding_conductor — we meter their usage for ops visibility but never cap/pause on it).
--
--   * ``tenant_agent_usage`` — TENANT-scoped RAW counters per (tenant, agent, month). RLS + FORCE
--     + a ``FOR ALL`` policy (USING + WITH CHECK): ``meter_llm_call`` does an UPSERT (INSERT ...
--     ON CONFLICT DO UPDATE), so a SELECT/INSERT-only policy would silently reject the ON CONFLICT
--     UPDATE arm. Enforcement meters on RAW counts (rate-independent); ₹ is derived downstream for
--     ops display only. ``topup_*`` per-period columns RAISE the ceiling (a top-up sale). The
--     ``*_notified_at`` stamps make the soft/hard incident emission once-per-period-per-agent.
--
-- Mirrors the mig 156 (incidents) / mig 164 (conversation_log) tenant-RLS idiom:
-- ``app_current_tenant()`` predicate + ``app_role`` grantee. No PII columns (raw counts only);
-- ON DELETE CASCADE off tenants(id) covers hard tenant deletion (no dsr_purge entry needed —
-- these are aggregate meters, not the owner's personal/conversation data).

-- === GLOBAL cap config ======================================================
CREATE TABLE agent_cost_limits (
    agent          TEXT PRIMARY KEY,
    max_api_calls  INT NOT NULL,
    max_tokens_in  BIGINT NOT NULL,
    max_tokens_out BIGINT NOT NULL,
    soft_pct       SMALLINT NOT NULL DEFAULT 80 CHECK (soft_pct BETWEEN 1 AND 100),
    enabled        BOOLEAN NOT NULL DEFAULT true,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE agent_cost_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_cost_limits FORCE ROW LEVEL SECURITY;
-- Readable by every tenant connection (global config — no tenant scope). No write policy: with
-- FORCE RLS + no permissive write policy, app_role INSERT/UPDATE/DELETE is rejected at the row
-- level; the BYPASSRLS service role (the migration runner + prod service key) mutates caps.
CREATE POLICY agent_cost_limits_select ON agent_cost_limits FOR SELECT USING (true);
GRANT SELECT ON agent_cost_limits TO app_role;

-- Seed — one uniform envelope for now (a POLICY placeholder Fazal will tune). The two SETUP agents
-- are enabled=false (tracked, UNBILLED): we meter their usage but never soft-notify / hard-pause.
INSERT INTO agent_cost_limits
    (agent, max_api_calls, max_tokens_in, max_tokens_out, soft_pct, enabled)
VALUES
    ('sales_recovery',       4000, 6800000, 2100000, 80, true),
    ('DEFAULT',              4000, 6800000, 2100000, 80, true),
    ('integration',          4000, 6800000, 2100000, 80, false),   -- tracked, UNBILLED (setup)
    ('onboarding_conductor', 4000, 6800000, 2100000, 80, false);   -- tracked, UNBILLED (setup)

-- === TENANT-scoped raw counters =============================================
CREATE TABLE tenant_agent_usage (
    tenant_id        UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    agent            TEXT NOT NULL,
    period_month     DATE NOT NULL,
    api_calls        INT NOT NULL DEFAULT 0,
    tokens_in        BIGINT NOT NULL DEFAULT 0,
    tokens_out       BIGINT NOT NULL DEFAULT 0,
    -- per-period top-up: raises the effective ceiling (base cap + topup) for THIS period only.
    topup_api_calls  INT NOT NULL DEFAULT 0,
    topup_tokens_in  BIGINT NOT NULL DEFAULT 0,
    topup_tokens_out BIGINT NOT NULL DEFAULT 0,
    -- once-per-period-per-agent notification stamps (soft warning / first hard block).
    soft_notified_at TIMESTAMPTZ,
    hard_notified_at TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, agent, period_month)
);

ALTER TABLE tenant_agent_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_agent_usage FORCE ROW LEVEL SECURITY;
-- FOR ALL (not SELECT/INSERT-only): the meter is an UPSERT (INSERT ... ON CONFLICT DO UPDATE), so
-- the policy MUST cover UPDATE — otherwise the ON CONFLICT UPDATE arm is silently rejected under
-- FORCE RLS and every second-and-later call for a (tenant, agent, month) would fail closed.
CREATE POLICY tenant_agent_usage_all ON tenant_agent_usage FOR ALL
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON tenant_agent_usage TO app_role;
