-- 145_vt507_discovery_cache.sql — VT-507: persistent 24h discovery cache +
-- entity discovery observability substrate.
--
-- discovery_cache: global (NOT tenant-scoped) persistent cache for the entity-discovery
-- legs (source='knowyourgst', source='llm'). Replaces the in-process 6h dict that wipes
-- on redeploy. 24h TTL enforced by expires_at; reads filter ON expires_at > NOW().
-- PK is (source, normalized_query) — one cache entry per source × query.
--
-- entity_discovery_requests: observability substrate for the async parallel discovery
-- (VT-507). One row per (discovery_id, source) attempt — records status, failure_reason,
-- impact, latency_ms, and the serialised candidates JSONB so the poll endpoint can return
-- them without knowing the original query string.
--
-- Neither table is tenant-scoped (discovery is a pre-tenant global registry lookup).
-- Service-role only via deny-all RLS — mirrors migrations/009 (env_config) convention.
-- The pool's Postgres superuser/service role bypasses RLS entirely.

CREATE TABLE IF NOT EXISTS discovery_cache (
    source           TEXT        NOT NULL,
    normalized_query TEXT        NOT NULL,
    response         JSONB       NOT NULL,
    expires_at       TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, normalized_query)
);

ALTER TABLE discovery_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE discovery_cache FORCE ROW LEVEL SECURITY;

CREATE POLICY discovery_cache_no_tenant_access ON discovery_cache
    FOR ALL USING (false) WITH CHECK (false);

-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS entity_discovery_requests (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    discovery_id     UUID        NOT NULL,
    source           TEXT        NOT NULL,
    status           TEXT        NOT NULL,       -- 'running' | 'complete' | 'error'
    failure_reason   TEXT        NULL,           -- 'timeout' | 'scrape_error' | 'parse_error' | 'no_key' | 'zero_results'
    impact           TEXT        NULL,           -- 'blocked_signup' | 'degraded_to_manual'
    dbos_workflow_id TEXT        NULL,
    latency_ms       INT         NULL,
    candidates       JSONB       NULL,           -- [EntityCandidate dicts] for the poll seam
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS entity_discovery_requests_discovery_id_idx
    ON entity_discovery_requests (discovery_id);

ALTER TABLE entity_discovery_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_discovery_requests FORCE ROW LEVEL SECURITY;

CREATE POLICY entity_discovery_requests_no_tenant_access ON entity_discovery_requests
    FOR ALL USING (false) WITH CHECK (false);
