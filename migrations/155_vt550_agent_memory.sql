-- 155_vt550_agent_memory.sql — VT-550 (C3b): the seedable learnable-memory MECHANISM.
--
-- CL-2026-07-01-no-fixed-playbook + Cowork 2026-07-01T22:00Z: knowledge = the LLM's reasoning + a
-- LEARNABLE memory the agent grows — NOT a fixed authored note-set (that's a cage, killed). To
-- shorten cold-start (CL-426 accelerant) the memory is SEEDED with archetype knowledge — but the
-- seed is MUTABLE SEED MEMORY the agent grows beyond, not a reference it is confined to.
--
-- This table is the MECHANISM (the seed CONTENT is a separate Fazal/archetype follow-up):
--   - GLOBAL rows (tenant_id IS NULL, memory_scope='global') = archetype seeds, a shared head-start
--     every tenant may READ (not private data). Only the SERVICE path writes them — a tenant can
--     never write a global seed (the INSERT policy is tenant-scoped).
--   - TENANT rows (tenant_id NOT NULL, memory_scope='tenant') = this tenant's own seed/learned
--     memory. ``source='learned'`` upserts OVERWRITE the seed for the same (tenant, agent, key),
--     version+1 — the "grow beyond the seed" posture.
--
-- CAPTURE/SEED-NOW, RETRIEVE-LATER: retrieval ACTIVATION (scope/authority/contradiction resolution)
-- is Phase-2, exactly like agent_corrections (VT-531). ``retrieval_eligible`` DEFAULTs false → the
-- retrieval interface returns nothing until Phase-2 flips it; no ALTER needed later.
--
-- Tenant rows are the subject's data → RLS + FORCE + operator SELECT + dsr_purge (WHERE tenant_id=…)
-- erases them; GLOBAL seeds (tenant_id NULL) are NOT a subject's data and survive erasure.

CREATE TABLE agent_memory (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NULL REFERENCES tenants (id) ON DELETE CASCADE,  -- NULL = global seed
    memory_scope       TEXT NOT NULL CHECK (memory_scope IN ('global', 'tenant')),
    source             TEXT NOT NULL CHECK (source IN ('seed', 'learned')),
    archetype          TEXT NOT NULL DEFAULT '',   -- global seeds: business archetype ('' = generic)
    agent              TEXT NOT NULL DEFAULT '',    -- lane the memory applies to ('' = cross-lane)
    memory_key         TEXT NOT NULL,               -- stable key for mutable upsert/overwrite
    content            TEXT NOT NULL,               -- PII-REDACTED substance
    weight             REAL NULL,                   -- learning may adjust salience
    version            INT NOT NULL DEFAULT 1,
    -- Retrieval-gate placeholders — DEFAULT-CLOSED, unused until Phase-2 activation (no ALTER later).
    retrieval_eligible BOOLEAN NOT NULL DEFAULT false,
    expires_at         TIMESTAMPTZ NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- a tenant row MUST carry a tenant_id; a global row MUST NOT.
    CONSTRAINT agent_memory_scope_tenant CHECK (
        (memory_scope = 'tenant' AND tenant_id IS NOT NULL)
        OR (memory_scope = 'global' AND tenant_id IS NULL)
    )
);
-- Mutable upsert keys: a learned entry OVERWRITES the seed for the same key ("grow beyond the seed").
CREATE UNIQUE INDEX agent_memory_tenant_key
    ON agent_memory (tenant_id, agent, memory_key) WHERE tenant_id IS NOT NULL;
CREATE UNIQUE INDEX agent_memory_global_key
    ON agent_memory (archetype, agent, memory_key) WHERE tenant_id IS NULL;
-- Phase-2 retrieval scans eligible rows; ready (default-closed today).
CREATE INDEX agent_memory_retrievable
    ON agent_memory (tenant_id, agent) WHERE retrieval_eligible;

ALTER TABLE agent_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_memory FORCE ROW LEVEL SECURITY;
-- SELECT: the tenant's own rows PLUS the shared global seeds (archetype head-start, not private data).
CREATE POLICY agent_memory_select ON agent_memory FOR SELECT
    USING (tenant_id = app_current_tenant() OR tenant_id IS NULL);
-- Writes are tenant-scoped ONLY — a tenant can never write a global seed (service path writes those).
CREATE POLICY agent_memory_insert ON agent_memory FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY agent_memory_update ON agent_memory FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY agent_memory_delete ON agent_memory FOR DELETE
    USING (tenant_id = app_current_tenant());

CREATE POLICY agent_memory_operator_select ON agent_memory
    AS PERMISSIVE FOR SELECT TO PUBLIC
    USING (
        COALESCE(
            NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'operator_claim',
            ''
        ) = 'true'
    );
