-- 187_vt719_manager_asserted_facts.sql — VT-719 S3: the Manager's ASSERTED-FACTS ledger.
--
-- WHAT: a durable, tenant-scoped record of the facts/commitments the Manager has TOLD the owner
-- (not what it knows — what it has SAID): fact_key + typed fact_value + the sentence + provenance.
-- WHY (CL-2026-07-28-single-voice-manager): "never contradict yourself" is only enforceable
-- against a record of what was said. Consulted at compose inside the VT-718 emission choke; a
-- flip of a prior assertion is only legal as an OWNED change ("earlier I said X — now Y
-- because…"). O8 join (CL-2026-07-28-o8-living-knowledge §12.3): derived_from_card_id pins the
-- exact immutable card version an assertion came from, so a card supersession can find every
-- tenant assertion derived from the old version and queue a proactive correction.
--
-- PRIVACY: tenant_id NOT NULL, ENABLE + FORCE RLS, full CRUD policies via app_current_tenant().
-- Registered in dsr_purge._PURGE_ORDER in this SAME change set (leaf; FK tenants CASCADE never
-- fires because DSR anonymizes the tenants row — explicit sweep is the erasure path).
-- statement_text carries owner-facing sentences only (no third-party PII by construction).
--
-- REVERSAL (not executed here): remove from _PURGE_ORDER, then DROP TABLE.

CREATE TABLE public.manager_asserted_facts (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES public.tenants (id) ON DELETE CASCADE,
    asserted_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    surface               TEXT NOT NULL DEFAULT 'manager'
                              CHECK (surface IN ('journey', 'manager', 'system', 'signup')),
    message_sid           TEXT NULL,
    fact_key              TEXT NOT NULL CHECK (btrim(fact_key) <> ''),
    fact_value            JSONB NOT NULL,
    statement_text        TEXT NOT NULL DEFAULT '',
    derived_from_card_id  UUID NULL
                              REFERENCES public.knowledge_cards (id) ON DELETE SET NULL,
    derived_from          JSONB NOT NULL DEFAULT '{}'::jsonb,
    status                TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'superseded', 'retracted')),
    superseded_by         UUID NULL
                              REFERENCES public.manager_asserted_facts (id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.manager_asserted_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.manager_asserted_facts FORCE ROW LEVEL SECURITY;

CREATE POLICY manager_asserted_facts_select ON public.manager_asserted_facts FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY manager_asserted_facts_insert ON public.manager_asserted_facts FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY manager_asserted_facts_update ON public.manager_asserted_facts FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY manager_asserted_facts_delete ON public.manager_asserted_facts FOR DELETE
    USING (tenant_id = app_current_tenant());

-- The contradiction-check read: latest ACTIVE assertion per fact_key for a tenant.
CREATE INDEX manager_asserted_facts_key_latest
    ON public.manager_asserted_facts (tenant_id, fact_key, asserted_at DESC);
-- The O8 §12.3 supersession sweep: assertions derived from a given card version.
CREATE INDEX manager_asserted_facts_card_fk
    ON public.manager_asserted_facts (derived_from_card_id)
    WHERE derived_from_card_id IS NOT NULL;
