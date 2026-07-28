-- 183_vt709_o8_tenant_evidence.sql — VT-709 O8 TENANT-SCOPED evidence + incidents.
--
-- WHAT: decision_evidence_links attributes retrieved card versions to tenant decisions/outcomes;
-- knowledge_incidents records suspected/confirmed harmful-card events with redacted text only.
-- WHY: causality/ablation needs tenant outcome evidence, but no tenant narrative may enter the
-- GLOBAL registry.  These two tables are the explicit privacy boundary.
--
-- PRIVACY: tenant_id UUID NOT NULL, ENABLE + FORCE RLS, complete CRUD policies scoped through
-- app_current_tenant().  Both tables are registered in dsr_purge._PURGE_ORDER in this VT-709
-- change set, decision_evidence_links before knowledge_incidents exactly as the canonical spec
-- requires.  The hard-delete canary asserts physical zero rows after DSR because the tenants row
-- is anonymized, not deleted, so ON DELETE CASCADE is NOT the erasure path.
--
-- REVERSAL (not executed here): remove both names from _PURGE_ORDER, then drop incidents followed
-- by decision links.  This migration is WRITTEN, NOT RUN by Codex.

CREATE TABLE public.decision_evidence_links (
    id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                  UUID NOT NULL REFERENCES public.tenants (id) ON DELETE CASCADE,
    run_id                     UUID NOT NULL,
    decision_id                TEXT NOT NULL CHECK (btrim(decision_id) <> ''),
    corpus_version_id          UUID NULL
                                   REFERENCES public.knowledge_corpus_versions (id) ON DELETE SET NULL,
    corpus_version_ref         UUID NOT NULL,
    card_id                    UUID NULL
                                   REFERENCES public.knowledge_cards (id) ON DELETE SET NULL,
    card_version_ref           UUID NOT NULL,
    retrieval_stage            TEXT NOT NULL
                                   CHECK (retrieval_stage IN (
                                       'triage', 'planning', 'specialist', 'review', 'verification'
                                   )),
    disposition                TEXT NOT NULL
                                   CHECK (disposition IN ('retrieved', 'selected', 'rejected')),
    semantic_score             DOUBLE PRECISION NULL
                                   CHECK (semantic_score BETWEEN 0.0 AND 1.0),
    lexical_score              DOUBLE PRECISION NULL
                                   CHECK (lexical_score BETWEEN 0.0 AND 1.0),
    entity_score               DOUBLE PRECISION NULL
                                   CHECK (entity_score BETWEEN 0.0 AND 1.0),
    combined_score             DOUBLE PRECISION NULL
                                   CHECK (combined_score BETWEEN 0.0 AND 1.0),
    observed_outcome_code      TEXT NULL,
    observed_outcome_score     DOUBLE PRECISION NULL,
    observed_at                TIMESTAMPTZ NULL,
    retention_class            TEXT NOT NULL DEFAULT 'tenant_lifetime'
                                   CHECK (btrim(retention_class) <> ''),
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT decision_evidence_links_attribution_uniq UNIQUE (
        tenant_id, run_id, decision_id, card_version_ref, disposition
    )
);

ALTER TABLE public.decision_evidence_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.decision_evidence_links FORCE ROW LEVEL SECURITY;

CREATE POLICY decision_evidence_links_select ON public.decision_evidence_links FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY decision_evidence_links_insert ON public.decision_evidence_links FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY decision_evidence_links_update ON public.decision_evidence_links FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY decision_evidence_links_delete ON public.decision_evidence_links FOR DELETE
    USING (tenant_id = app_current_tenant());

CREATE INDEX decision_evidence_links_tenant_run
    ON public.decision_evidence_links (tenant_id, run_id, decision_id);
CREATE INDEX decision_evidence_links_corpus_fk
    ON public.decision_evidence_links (corpus_version_id);
CREATE INDEX decision_evidence_links_card_fk
    ON public.decision_evidence_links (card_id);
CREATE INDEX decision_evidence_links_card_ref
    ON public.decision_evidence_links (tenant_id, card_version_ref);


CREATE TABLE public.knowledge_incidents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES public.tenants (id) ON DELETE CASCADE,
    incident_class      TEXT NOT NULL
                            CHECK (incident_class IN (
                                'money', 'regulatory', 'consent', 'cross_tenant', 'privacy',
                                'decision_quality', 'latency_cost', 'provenance_loss', 'other'
                            )),
    card_id             UUID NULL
                            REFERENCES public.knowledge_cards (id) ON DELETE SET NULL,
    card_version_ref    UUID NOT NULL,
    evidence_refs       UUID[] NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'suspected'
                            CHECK (status IN ('suspected', 'confirmed', 'resolved', 'dismissed')),
    quarantine_action   TEXT NOT NULL DEFAULT 'none'
                            CHECK (quarantine_action IN ('none', 'requested', 'applied', 'reverted')),
    detail_redacted     TEXT NULL,
    resolution_redacted TEXT NULL,
    retention_class     TEXT NOT NULL DEFAULT 'tenant_lifetime'
                            CHECK (btrim(retention_class) <> ''),
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_incidents_resolution_time CHECK (
        resolved_at IS NULL OR resolved_at >= detected_at
    )
);

ALTER TABLE public.knowledge_incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.knowledge_incidents FORCE ROW LEVEL SECURITY;

CREATE POLICY knowledge_incidents_select ON public.knowledge_incidents FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY knowledge_incidents_insert ON public.knowledge_incidents FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY knowledge_incidents_update ON public.knowledge_incidents FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY knowledge_incidents_delete ON public.knowledge_incidents FOR DELETE
    USING (tenant_id = app_current_tenant());

CREATE INDEX knowledge_incidents_tenant_status
    ON public.knowledge_incidents (tenant_id, status, detected_at);
CREATE INDEX knowledge_incidents_card_fk
    ON public.knowledge_incidents (card_id);
CREATE INDEX knowledge_incidents_card_ref
    ON public.knowledge_incidents (tenant_id, card_version_ref);

COMMENT ON TABLE public.decision_evidence_links IS
    'VT-709 O8 TENANT attribution: ids/scores only; FORCE RLS; DSR hard-delete required.';
COMMENT ON TABLE public.knowledge_incidents IS
    'VT-709 O8 TENANT harmful-card incidents; free text redacted before write; FORCE RLS; DSR purge.';
