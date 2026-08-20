-- 189_vt726_card_retrieval_projection.sql — VT-726 O8 single-table retrieval projection.
--
-- WHAT: add the fields required to reconstruct ``KnowledgeCard`` directly from one immutable
-- ``knowledge_cards`` row: domain, source class, source rights, source/provenance identity,
-- corroboration count, retrieval eligibility, and corpus-version identity.
-- WHY: card retrieval filters and ranks these fields on every request.  A normal view would only
-- hide the same three-table join and a materialized view would introduce refresh state.  Because
-- the registry was verified empty before this migration was authored, denormalizing now is the
-- smallest reliable boundary and requires no guessed backfill.
--
-- OWNERSHIP/PRIVACY: ``knowledge_cards`` remains GLOBAL, tenant-free and service-write-only.  No
-- raw source expression is stored here; provenance is metadata only.  These columns inherit the
-- table's immutable-row trigger and the write revocation established by migration 182.
-- ADMISSION: ``retrieval_eligible`` is card-level eligibility only.  It does not imply that the
-- containing corpus passed O11; VT-726 leaves its corpus admission_verdict ``pending``/shadow.
-- RIGHTS-REMOVAL FIX: VT-726 creates a real v1 -> v2 supersession chain and therefore exposes a
-- latent migration-182 conflict: the immutable-row trigger blocks its own FK's nested ON DELETE
-- SET NULL.  The replacement below permits only that nested FK action, only old-id -> NULL, and
-- only when supersedes_card_id is the sole changed column. A direct imitation UPDATE stays blocked.
-- REVERSAL (not executed): restore migration 182's trigger function, drop the two indexes, then
-- drop the eight columns and their constraints.
-- This migration is WRITTEN, NOT RUN by Codex.  CC executes it in dev with --expected-env dev.

ALTER TABLE public.knowledge_cards
    ADD COLUMN domain TEXT NOT NULL
        CHECK (domain IN (
            'management', 'sales', 'marketing', 'compliance', 'finance', 'accounting',
            'operations', 'onboarding', 'integration', 'technology', 'cost_optimization',
            'cross_functional'
        )),
    ADD COLUMN source_class TEXT NOT NULL
        CHECK (source_class IN ('t1', 't1v', 't2', 't3', 't4')),
    ADD COLUMN usage_rights JSONB NOT NULL
        CHECK (
            jsonb_typeof(usage_rights) = 'object'
            AND usage_rights ? 'status'
            AND usage_rights->>'status' IN (
                'open_licensed', 'public_domain', 'permission_granted', 'live_link_only',
                'restricted', 'unknown'
            )
        ),
    ADD COLUMN independence_cluster TEXT NOT NULL
        CHECK (btrim(independence_cluster) <> ''),
    ADD COLUMN corroboration_cluster_count INT NOT NULL DEFAULT 1
        CHECK (corroboration_cluster_count >= 1),
    ADD COLUMN provenance JSONB NOT NULL
        CHECK (
            jsonb_typeof(provenance) = 'object'
            AND jsonb_typeof(provenance->'source_ids') = 'array'
            AND jsonb_array_length(provenance->'source_ids') >= 1
            AND btrim(provenance->>'publisher') <> ''
            AND provenance ? 'retrieved_at'
            AND jsonb_typeof(provenance->'tainted') = 'boolean'
        ),
    ADD COLUMN retrieval_eligible BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN corpus_version_id UUID NULL
        REFERENCES public.knowledge_corpus_versions (id) ON DELETE RESTRICT;

CREATE INDEX knowledge_cards_domain_status
    ON public.knowledge_cards (domain, status);
CREATE INDEX knowledge_cards_corpus_version_fk
    ON public.knowledge_cards (corpus_version_id);

CREATE OR REPLACE FUNCTION public.knowledge_cards_immutable_row()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF pg_trigger_depth() > 1
           AND OLD.supersedes_card_id IS NOT NULL
           AND NEW.supersedes_card_id IS NULL
           AND (to_jsonb(NEW) - 'supersedes_card_id') IS NOT DISTINCT FROM
               (to_jsonb(OLD) - 'supersedes_card_id')
        THEN
            RETURN NEW;
        END IF;
    END IF;

    RAISE EXCEPTION
        'knowledge_cards rows are immutable (VT-709/VT-726); insert a new version instead of %',
        TG_OP;
END;
$$;

COMMENT ON COLUMN public.knowledge_cards.domain IS
    'VT-726 first-class KnowledgeDomain used by the retrieval policy first filter.';
COMMENT ON COLUMN public.knowledge_cards.provenance IS
    'VT-726 source metadata sufficient for one-row KnowledgeCard reconstruction; never raw text.';
COMMENT ON COLUMN public.knowledge_cards.retrieval_eligible IS
    'VT-726 card eligibility for advisory retrieval; not effect authority or corpus admission.';
