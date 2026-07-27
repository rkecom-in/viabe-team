-- 182_vt709_o8_card_registry.sql — VT-709 O8 governed GLOBAL card registry.
--
-- WHAT: seven tenant-free tables for sources, immutable card versions, provenance edges, corpus
-- snapshots, evaluation/ablation records, and append-only lifecycle history.
-- WHY: O8 needs a versioned, reversible knowledge substrate whose claims remain attributable and
-- whose global rows structurally cannot carry tenant identity.  Raw source bodies are deliberately
-- absent: only rights-reviewed metadata and distilled cards enter this registry.
--
-- PRIVACY/OWNERSHIP: these tables have NO tenant_id column and NO RLS.  Migration 015 grants
-- app_role DML on future tables by default, so this migration explicitly REVOKES global writes
-- from app_role; only the privileged service/migration role may curate them.  Live reads are not
-- granted here because VT-709 is inert.
--
-- IMMUTABILITY: knowledge_cards rejects UPDATE (a changed claim/state is a new version row).
-- knowledge_lifecycle_events rejects UPDATE/DELETE/TRUNCATE for every role.  Rights-required hard
-- deletion remains possible for cards/sources; lifecycle rows preserve the immutable card UUID in
-- card_version_ref even after their nullable FK is SET NULL.
--
-- REVERSAL (not executed here): drop lifecycle/card immutability triggers + functions, then drop
-- the seven tables in reverse dependency order.  This migration is WRITTEN, NOT RUN by Codex.

CREATE TABLE public.knowledge_sources (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_url    TEXT NOT NULL,
    publisher        TEXT NOT NULL,
    source_class     TEXT NOT NULL
                         CHECK (source_class IN ('t1', 't1v', 't2', 't3', 't4')),
    content_hash     TEXT NOT NULL
                         CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    acquired_at      TIMESTAMPTZ NOT NULL,
    usage_rights     JSONB NOT NULL
                         CHECK (
                             jsonb_typeof(usage_rights) = 'object'
                             AND usage_rights ? 'status'
                             AND usage_rights->>'status' IN (
                                 'open_licensed', 'public_domain', 'permission_granted',
                                 'live_link_only', 'restricted', 'unknown'
                             )
                         ),
    retention_class  TEXT NOT NULL CHECK (btrim(retention_class) <> ''),
    tainted           BOOLEAN NOT NULL DEFAULT true,
    expires_at        TIMESTAMPTZ NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_sources_content_hash_uniq UNIQUE (content_hash),
    CONSTRAINT knowledge_sources_t4_expiry CHECK (
        source_class <> 't4'
        OR (expires_at IS NOT NULL AND expires_at <= acquired_at + INTERVAL '6 months')
    )
);

CREATE INDEX knowledge_sources_class_retention
    ON public.knowledge_sources (source_class, retention_class);


CREATE TABLE public.knowledge_cards (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Stable across immutable versions of the same logical card.  Distinct conflicting cards for
    -- one claim_key keep distinct card_key values, so conflict visibility is not collapsed.
    card_key                 UUID NOT NULL,
    version                  INT NOT NULL CHECK (version >= 1),
    claim                    TEXT NOT NULL CHECK (btrim(claim) <> ''),
    claim_key                TEXT NOT NULL CHECK (btrim(claim_key) <> ''),
    claim_value              JSONB NOT NULL
                                 CHECK (
                                     jsonb_typeof(claim_value) = 'object'
                                     AND claim_value ? 'value_type'
                                     AND claim_value ? 'value'
                                     AND claim_value->>'value_type' IN (
                                         'text', 'integer', 'decimal', 'boolean', 'date', 'datetime'
                                     )
                                 ),
    distillation_note        TEXT NOT NULL CHECK (btrim(distillation_note) <> ''),
    jurisdictions            TEXT[] NOT NULL DEFAULT '{}',
    size_bands               TEXT[] NOT NULL DEFAULT '{}',
    industries               TEXT[] NOT NULL DEFAULT '{}',
    maturity_stages          TEXT[] NOT NULL DEFAULT '{}',
    channels                 TEXT[] NOT NULL DEFAULT '{}',
    applicability_universal  BOOLEAN NOT NULL DEFAULT false,
    effective_from           TIMESTAMPTZ NULL,
    effective_until          TIMESTAMPTZ NULL,
    authority                TEXT NOT NULL
                                 CHECK (authority IN (
                                     'owner', 'verified_system', 'vtr', 'verified_outcome',
                                     'seed', 'agent_inference'
                                 )),
    confidence               TEXT NOT NULL
                                 CHECK (confidence IN ('low', 'medium', 'high', 'verified')),
    scope                    TEXT NOT NULL CHECK (scope IN ('global', 'prior')),
    status                   TEXT NOT NULL
                                 CHECK (status IN (
                                     'candidate', 'validated', 'disputed', 'superseded', 'expired',
                                     'quarantined', 'research_only'
                                 )),
    retention_class          TEXT NOT NULL CHECK (btrim(retention_class) <> ''),
    tainted                  BOOLEAN NOT NULL DEFAULT true,
    expires_at               TIMESTAMPTZ NULL,
    supersedes_card_id       UUID NULL
                                 REFERENCES public.knowledge_cards (id) ON DELETE SET NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_cards_key_version_uniq UNIQUE (card_key, version),
    CONSTRAINT knowledge_cards_effective_window CHECK (
        effective_from IS NULL OR effective_until IS NULL OR effective_until >= effective_from
    ),
    CONSTRAINT knowledge_cards_universal_dimensions CHECK (
        NOT applicability_universal
        OR (
            cardinality(jurisdictions) = 0
            AND cardinality(size_bands) = 0
            AND cardinality(industries) = 0
            AND cardinality(maturity_stages) = 0
            AND cardinality(channels) = 0
        )
    ),
    CONSTRAINT knowledge_cards_no_self_supersession CHECK (supersedes_card_id IS DISTINCT FROM id)
);

CREATE INDEX knowledge_cards_supersedes_fk
    ON public.knowledge_cards (supersedes_card_id);
CREATE INDEX knowledge_cards_claim_status
    ON public.knowledge_cards (claim_key, status);
CREATE INDEX knowledge_cards_scope_status
    ON public.knowledge_cards (scope, status);
CREATE INDEX knowledge_cards_validated
    ON public.knowledge_cards (claim_key, scope)
    WHERE status = 'validated';


CREATE TABLE public.knowledge_card_sources (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    card_id                  UUID NOT NULL
                                 REFERENCES public.knowledge_cards (id) ON DELETE CASCADE,
    source_id                UUID NOT NULL
                                 REFERENCES public.knowledge_sources (id) ON DELETE CASCADE,
    independence_cluster_id  TEXT NOT NULL CHECK (btrim(independence_cluster_id) <> ''),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_card_sources_edge_uniq UNIQUE (card_id, source_id)
);

CREATE INDEX knowledge_card_sources_card_fk
    ON public.knowledge_card_sources (card_id);
CREATE INDEX knowledge_card_sources_source_fk
    ON public.knowledge_card_sources (source_id);
CREATE INDEX knowledge_card_sources_cluster
    ON public.knowledge_card_sources (independence_cluster_id);


CREATE TABLE public.knowledge_corpus_versions (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version                   INT NOT NULL UNIQUE CHECK (version >= 1),
    parent_corpus_version_id  UUID NULL
                                  REFERENCES public.knowledge_corpus_versions (id) ON DELETE SET NULL,
    content_digest            TEXT NOT NULL UNIQUE
                                  CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    status                    TEXT NOT NULL DEFAULT 'draft'
                                  CHECK (status IN (
                                      'draft', 'candidate', 'shadow', 'validated', 'superseded',
                                      'rolled_back'
                                  )),
    admission_verdict         TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (admission_verdict IN (
                                      'pending', 'passed', 'failed', 'rolled_back'
                                  )),
    created_by                TEXT NOT NULL CHECK (btrim(created_by) <> ''),
    activated_at              TIMESTAMPTZ NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX knowledge_corpus_versions_parent_fk
    ON public.knowledge_corpus_versions (parent_corpus_version_id);
CREATE INDEX knowledge_corpus_versions_status
    ON public.knowledge_corpus_versions (status, admission_verdict);


CREATE TABLE public.knowledge_corpus_members (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    corpus_version_id  UUID NOT NULL
                           REFERENCES public.knowledge_corpus_versions (id) ON DELETE CASCADE,
    card_id            UUID NOT NULL
                           REFERENCES public.knowledge_cards (id) ON DELETE CASCADE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_corpus_members_membership_uniq UNIQUE (corpus_version_id, card_id)
);

CREATE INDEX knowledge_corpus_members_corpus_fk
    ON public.knowledge_corpus_members (corpus_version_id);
CREATE INDEX knowledge_corpus_members_card_fk
    ON public.knowledge_corpus_members (card_id);


CREATE TABLE public.knowledge_evaluations (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    corpus_version_id           UUID NOT NULL
                                    REFERENCES public.knowledge_corpus_versions (id) ON DELETE CASCADE,
    baseline_corpus_version_id  UUID NULL
                                    REFERENCES public.knowledge_corpus_versions (id) ON DELETE SET NULL,
    card_id                     UUID NULL
                                    REFERENCES public.knowledge_cards (id) ON DELETE SET NULL,
    card_version_ref            UUID NULL,
    evaluation_kind             TEXT NOT NULL
                                    CHECK (evaluation_kind IN (
                                        'baseline', 'treatment', 'ablation', 'safety_slice'
                                    )),
    dataset_partition           TEXT NOT NULL
                                    CHECK (dataset_partition IN (
                                        'development', 'validation', 'sealed'
                                    )),
    run_ref                     TEXT NOT NULL CHECK (btrim(run_ref) <> ''),
    evaluator_id                TEXT NOT NULL CHECK (btrim(evaluator_id) <> ''),
    sample_size                 INT NOT NULL CHECK (sample_size >= 0),
    metrics                     JSONB NOT NULL DEFAULT '{}'::jsonb
                                    CHECK (jsonb_typeof(metrics) = 'object'),
    passed                      BOOLEAN NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_evaluations_ablation_card CHECK (
        evaluation_kind <> 'ablation' OR card_version_ref IS NOT NULL
    ),
    CONSTRAINT knowledge_evaluations_run_uniq UNIQUE (
        corpus_version_id, evaluation_kind, run_ref, card_version_ref
    )
);

CREATE INDEX knowledge_evaluations_corpus_fk
    ON public.knowledge_evaluations (corpus_version_id);
CREATE INDEX knowledge_evaluations_baseline_corpus_fk
    ON public.knowledge_evaluations (baseline_corpus_version_id);
CREATE INDEX knowledge_evaluations_card_fk
    ON public.knowledge_evaluations (card_id);


CREATE TABLE public.knowledge_lifecycle_events (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Nullable only for the explicit usage-rights hard-delete exception.  card_version_ref remains
    -- immutable and attributable after ON DELETE SET NULL preserves the event.
    card_id           UUID NULL
                          REFERENCES public.knowledge_cards (id) ON DELETE SET NULL,
    card_version_ref  UUID NOT NULL,
    event_type        TEXT NOT NULL
                          CHECK (event_type IN (
                              'promotion', 'dispute', 'quarantine', 'supersession', 'expiry',
                              'rollback', 'rights_removal', 'reinstatement'
                          )),
    from_status       TEXT NULL
                          CHECK (from_status IS NULL OR from_status IN (
                              'candidate', 'validated', 'disputed', 'superseded', 'expired',
                              'quarantined', 'research_only'
                          )),
    to_status         TEXT NOT NULL
                          CHECK (to_status IN (
                              'candidate', 'validated', 'disputed', 'superseded', 'expired',
                              'quarantined', 'research_only'
                          )),
    actor_id          TEXT NOT NULL CHECK (btrim(actor_id) <> ''),
    reason            TEXT NOT NULL CHECK (btrim(reason) <> ''),
    idempotency_key   TEXT NOT NULL UNIQUE CHECK (btrim(idempotency_key) <> ''),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX knowledge_lifecycle_events_card_fk
    ON public.knowledge_lifecycle_events (card_id);
CREATE INDEX knowledge_lifecycle_events_card_ref
    ON public.knowledge_lifecycle_events (card_version_ref, created_at);


CREATE OR REPLACE FUNCTION public.knowledge_cards_immutable_row()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'knowledge_cards rows are immutable (VT-709); insert a new version instead of %',
        TG_OP;
END;
$$;

CREATE TRIGGER knowledge_cards_no_update
    BEFORE UPDATE ON public.knowledge_cards
    FOR EACH ROW EXECUTE FUNCTION public.knowledge_cards_immutable_row();


CREATE OR REPLACE FUNCTION public.knowledge_cards_rights_delete_only()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.knowledge_lifecycle_events
        WHERE card_version_ref = OLD.id
          AND event_type = 'rights_removal'
    ) THEN
        RAISE EXCEPTION
            'knowledge_cards hard-delete requires a prior rights_removal lifecycle event (VT-709)';
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER knowledge_cards_rights_delete_guard
    BEFORE DELETE ON public.knowledge_cards
    FOR EACH ROW EXECUTE FUNCTION public.knowledge_cards_rights_delete_only();


CREATE OR REPLACE FUNCTION public.knowledge_lifecycle_events_append_only()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- ``knowledge_lifecycle_events.card_id`` is deliberately nullable so a rights-required hard
    -- delete can preserve the immutable ``card_version_ref`` audit tombstone.  PostgreSQL performs
    -- ON DELETE SET NULL as a nested UPDATE; allow ONLY that FK action and ONLY when card_id is the
    -- sole changed column.  A direct UPDATE has trigger depth 1 and remains blocked.  Comparing the
    -- complete JSON row minus card_id keeps this fail-closed if columns are added later.
    IF TG_OP = 'UPDATE' THEN
        IF pg_trigger_depth() > 1
           AND OLD.card_id IS NOT NULL
           AND NEW.card_id IS NULL
           AND (to_jsonb(NEW) - 'card_id') IS NOT DISTINCT FROM
               (to_jsonb(OLD) - 'card_id')
        THEN
            RETURN NEW;
        END IF;
    END IF;

    RAISE EXCEPTION
        'knowledge_lifecycle_events is append-only (VT-709); % blocked',
        TG_OP;
END;
$$;

CREATE TRIGGER knowledge_lifecycle_events_no_row_mutate
    BEFORE UPDATE OR DELETE ON public.knowledge_lifecycle_events
    FOR EACH ROW EXECUTE FUNCTION public.knowledge_lifecycle_events_append_only();

CREATE TRIGGER knowledge_lifecycle_events_no_truncate
    BEFORE TRUNCATE ON public.knowledge_lifecycle_events
    FOR EACH STATEMENT EXECUTE FUNCTION public.knowledge_lifecycle_events_append_only();


-- Migration 015's ALTER DEFAULT PRIVILEGES would otherwise make global curation tenant-app
-- writable.  Revoke every mutating privilege; SELECT remains ungranted until O8 activation.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON
    public.knowledge_sources,
    public.knowledge_cards,
    public.knowledge_card_sources,
    public.knowledge_corpus_versions,
    public.knowledge_corpus_members,
    public.knowledge_evaluations,
    public.knowledge_lifecycle_events
FROM app_role;

REVOKE ALL ON
    public.knowledge_sources,
    public.knowledge_cards,
    public.knowledge_card_sources,
    public.knowledge_corpus_versions,
    public.knowledge_corpus_members,
    public.knowledge_evaluations,
    public.knowledge_lifecycle_events
FROM PUBLIC;

COMMENT ON TABLE public.knowledge_sources IS
    'VT-709 O8 GLOBAL source metadata and usage rights. No tenant_id; no raw source body.';
COMMENT ON TABLE public.knowledge_cards IS
    'VT-709 O8 GLOBAL immutable card versions. No tenant_id; UPDATE rejected; new state is a new row.';
COMMENT ON TABLE public.knowledge_lifecycle_events IS
    'VT-709 O8 GLOBAL append-only card lifecycle. card_version_ref survives rights-required deletion.';
