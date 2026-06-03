-- 086_l4_skill_corpus.sql — VT-70 L4 skill corpus (global, retrieval-augmented).
--
-- L4 = hand-authored domain knowledge the agent RETRIEVES at reasoning time
-- (Pillar 4: retrieve, don't calculate; Pillar 5: no fine-tuning — RAG context,
-- not weights). Static documents authored by Fazal/Clau (VT-313 content task);
-- VT-70 ships the pipeline + a synthetic placeholder seed.
--
-- NOT tenant-scoped — one global corpus (Pillar 8). No RLS: it is workspace-wide
-- domain wisdom, identical for every tenant, carries no per-tenant/customer data.
-- Embedding = voyage-4-lite vector(1024) + HNSW vector_cosine_ops (ground truth:
-- mig 019 L1 / VT-7.1 — NOT ada-002/1536/IVFFLAT, the spec's stale detail).
-- Claimed via scripts/migration_id_allocate.py (CL-424).

CREATE TABLE l4_documents (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title                     TEXT NOT NULL,
    body                      TEXT NOT NULL,
    body_embedding            vector(1024),
    tags                      TEXT[] NOT NULL DEFAULT '{}',
    applies_to_business_types TEXT[],            -- NULL = applies to all
    applies_to_city_tiers     TEXT[],            -- NULL = applies to all
    priority                  INTEGER NOT NULL DEFAULT 3
                              CHECK (priority BETWEEN 1 AND 5),
    authored_by               TEXT NOT NULL,
    version                   INTEGER NOT NULL DEFAULT 1,
    superseded_by             UUID REFERENCES l4_documents (id),
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent re-seed key: a doc is identified by its title + version.
CREATE UNIQUE INDEX l4_documents_title_version_uniq
    ON l4_documents (title, version);

-- ANN search (cosine ``<=>``) over the embedding.
CREATE INDEX l4_documents_embedding_hnsw
    ON l4_documents USING hnsw (body_embedding vector_cosine_ops);

-- Array applicability filters.
CREATE INDEX l4_documents_business_types_gin
    ON l4_documents USING gin (applies_to_business_types);
CREATE INDEX l4_documents_city_tiers_gin
    ON l4_documents USING gin (applies_to_city_tiers);
