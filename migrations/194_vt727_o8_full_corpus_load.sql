-- 194_vt727_o8_full_corpus_load.sql — VT-727 restart-safe full O8 corpus substrate.
--
-- WHAT: create the GLOBAL persisted-embedding table needed by the governed 118-record corpus.
-- The full records/sources/cards/corpus-membership load itself remains in the VT-727 canary and
-- registry writer: it must pass through the real VT-710 pipeline and reconcile every disposition,
-- not become a hand-authored SQL seed that revives the retired authored-playbook path.
--
-- WHY: VT-726 proved retrieval with process-local vectors, but those vectors are regenerated after
-- every worker restart and provider failure can empty a turn's candidate pool.  An embedding here
-- is bound to one immutable knowledge_cards version, the pinned Voyage model/dimension, and the
-- digest of that card's independently authored claim + distillation.  It is a retrieval cache,
-- never knowledge admission, corpus graduation, prompt injection, or effect authorization.
--
-- OWNERSHIP/PRIVACY: GLOBAL and tenant-free, like migrations 182/189.  The table contains vectors
-- of already-governed global cards only; no raw archived source body and no tenant/customer data.
-- It therefore has no tenant_id, RLS policy, DSR purge registration, or cross-tenant write path.
-- Migration 015 grants app_role privileges on future tables, so mutation is revoked explicitly;
-- app_role receives SELECT only for advisory shadow retrieval.  Curation stays service-only.
--
-- IMMUTABILITY/DELETION: knowledge_cards rows reject UPDATE, so (card_id, content_digest) cannot
-- become stale in place.  A changed claim is a new card version and therefore a new embedding row.
-- Rights-required hard deletion of a card cascades to this derived vector.  There is deliberately
-- no DELETE-blocking trigger here because it would fight that governed FK cascade.
--
-- INDEX: HNSW + vector_cosine_ops matches the pinned 1024-dimensional voyage-4-lite contract and
-- the repository's cosine-distance retrieval convention.  Exact card-id joins work immediately;
-- the ANN index keeps a later DB-side candidate prefilter from requiring another schema migration.
--
-- REVERSAL (not executed): mark corpus version 2 rolled_back so serving falls back to the prior
-- shadow snapshot, then DROP TABLE public.knowledge_card_embeddings.  Immutable registry history
-- remains attributable; rollback never rewrites or silently deletes knowledge history.
--
-- ALLOCATION: 194 was allocated by Clau for VT-727.  Migration 195 is intentionally unused: the
-- full corpus uses the existing registry transaction and this is its only new schema/rollback unit.
-- This migration is WRITTEN, NOT RUN by Codex.  CC executes it in dev with --expected-env dev.

CREATE TABLE public.knowledge_card_embeddings (
    card_id               UUID PRIMARY KEY
                              REFERENCES public.knowledge_cards (id) ON DELETE CASCADE,
    embedding_model       TEXT NOT NULL
                              CHECK (embedding_model = 'voyage-4-lite'),
    embedding_dimensions  INT NOT NULL
                              CHECK (embedding_dimensions = 1024),
    content_digest        TEXT NOT NULL
                              CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    embedding             vector(1024) NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX knowledge_card_embeddings_hnsw
    ON public.knowledge_card_embeddings USING hnsw (embedding vector_cosine_ops);

REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON public.knowledge_card_embeddings FROM app_role;
GRANT SELECT ON public.knowledge_card_embeddings TO app_role;
REVOKE ALL ON public.knowledge_card_embeddings FROM PUBLIC;

COMMENT ON TABLE public.knowledge_card_embeddings IS
    'VT-727 GLOBAL restart-safe vectors for immutable governed cards. No tenant data; advisory only.';
COMMENT ON COLUMN public.knowledge_card_embeddings.content_digest IS
    'SHA-256 of claim + newline + distillation_note; a mismatch forces fail-soft re-embedding.';
