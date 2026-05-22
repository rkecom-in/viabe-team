-- 019_l1_knowledge_graph.sql — L1 Knowledge Graph: per-tenant business
-- knowledge as an entity/relationship graph (VT-7.1 / CL-324).
--
-- L1 is hand-built relational + pgvector — NOT Apache AGE, NOT Mem0. Apache
-- AGE was retired per CL-324; Mem0 is reserved for L2/L3 (post-launch spike).
-- L1's input is already-structured foreign-keyed data (customers, products,
-- segments, rules, transactions), so a graph extension adds no retrieval
-- value over recursive-CTE traversal of a tenant-scoped relationship table.
--
-- ``valid_from`` / ``valid_to`` are plain data columns — the validity window
-- of a business fact. Time-aware DATA MODELLING, NOT Temporal-the-workflow-
-- product (rejected per CL-27). DBOS remains the durable execution substrate.
--
-- Embedding dimension: 1024, matching Voyage voyage-4-lite (Phase-1 pin —
-- pgvector had no prior usage on main; dimension established here by VT-7.1).
-- Switching models later requires a coordinated re-embed migration; do not
-- pick a new model without updating this comment + the population pipeline.
--
-- Index choice: HNSW with vector_cosine_ops for ANN search. Recall > IVFFlat
-- at Phase-1 row counts (<100k per tenant) and supports updates without
-- retraining. Retrieval MUST use the ``<=>`` (cosine distance) operator to
-- match the opclass.

CREATE TABLE l1_entities (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants (id),
    entity_type TEXT NOT NULL,
    attributes  JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding   vector(1024),
    valid_from  TIMESTAMPTZ,
    valid_to    TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pillar 3: RLS in the same migration that creates the table.
ALTER TABLE l1_entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE l1_entities FORCE ROW LEVEL SECURITY;

CREATE POLICY l1_entities_select ON l1_entities FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY l1_entities_insert ON l1_entities FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY l1_entities_update ON l1_entities FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY l1_entities_delete ON l1_entities FOR DELETE
    USING (tenant_id = app_current_tenant());

-- Indexes for the retrieval contract.
-- HNSW on embedding (vector_cosine_ops) — ANN search via the ``<=>`` operator.
CREATE INDEX l1_entities_embedding_hnsw
    ON l1_entities USING hnsw (embedding vector_cosine_ops);
-- Relational filter — tenant-scoped entity-type lookups.
CREATE INDEX l1_entities_tenant_type
    ON l1_entities (tenant_id, entity_type);
-- JSONB attribute filters (e.g. attributes->'locality' for L3 coarsening).
CREATE INDEX l1_entities_attributes_gin
    ON l1_entities USING gin (attributes jsonb_path_ops);


CREATE TABLE l1_relationships (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants (id),
    from_entity       UUID NOT NULL REFERENCES l1_entities (id),
    to_entity         UUID NOT NULL REFERENCES l1_entities (id),
    relationship_type TEXT NOT NULL,
    attributes        JSONB NOT NULL DEFAULT '{}'::jsonb,
    valid_from        TIMESTAMPTZ,
    valid_to          TIMESTAMPTZ
);

ALTER TABLE l1_relationships ENABLE ROW LEVEL SECURITY;
ALTER TABLE l1_relationships FORCE ROW LEVEL SECURITY;

CREATE POLICY l1_relationships_select ON l1_relationships FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY l1_relationships_insert ON l1_relationships FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY l1_relationships_update ON l1_relationships FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY l1_relationships_delete ON l1_relationships FOR DELETE
    USING (tenant_id = app_current_tenant());

-- Traversal indexes — recursive CTE over (tenant_id, from_entity) /
-- (tenant_id, to_entity). The composite ordering puts tenant_id first so
-- the RLS-filtered prefix scan is cheap on multi-tenant rows.
CREATE INDEX l1_relationships_tenant_from
    ON l1_relationships (tenant_id, from_entity);
CREATE INDEX l1_relationships_tenant_to
    ON l1_relationships (tenant_id, to_entity);
