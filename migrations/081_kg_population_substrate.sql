-- 081_kg_population_substrate.sql — VT-65 PR-1: L1 KG population substrate.
--
-- (a) kg_events_processed — idempotency ledger for the population consumer. The
--     consumer checks before processing; a duplicate event_id is a no-op
--     (replay-safe after a crash / re-backfill).
-- (b) l1_entities.external_key + UNIQUE(tenant_id, entity_type, external_key) —
--     the stable natural key (the source row's id) for idempotent node upsert.
--     Partial (external_key IS NOT NULL) so the existing single-entity rows
--     (business_profile / agent_reflection, no external_key) are unaffected.
-- (c) UNIQUE edge on l1_relationships — idempotent add_relationship.
--
-- All tenant-scoped (Pillar 3). Claimed via scripts/migration_id_allocate.py (CL-424).

CREATE TABLE kg_events_processed (
    event_id     UUID PRIMARY KEY,
    event_type   TEXT NOT NULL,
    tenant_id    UUID NOT NULL REFERENCES tenants (id),
    status       TEXT NOT NULL CHECK (status IN ('processed', 'failed')),
    error        TEXT,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE kg_events_processed ENABLE ROW LEVEL SECURITY;
ALTER TABLE kg_events_processed FORCE ROW LEVEL SECURITY;

CREATE POLICY kg_events_processed_select ON kg_events_processed FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY kg_events_processed_insert ON kg_events_processed FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY kg_events_processed_update ON kg_events_processed FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

CREATE INDEX kg_events_processed_tenant_idx ON kg_events_processed (tenant_id);

-- (b) natural key for idempotent node upsert.
ALTER TABLE l1_entities ADD COLUMN external_key TEXT;
CREATE UNIQUE INDEX l1_entities_natural_key
    ON l1_entities (tenant_id, entity_type, external_key)
    WHERE external_key IS NOT NULL;

-- (c) idempotent edges.
CREATE UNIQUE INDEX l1_relationships_edge_uniq
    ON l1_relationships (tenant_id, from_entity, to_entity, relationship_type);
