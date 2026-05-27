-- 029_l0_fragments.sql — VT-126 L0 memory substrate.
--
-- L0 = workspace operational memory for the orchestrator-agent (CL-26).
-- Per CL-324 LOCKED: L0 stays custom (L1 hand-built; L2/L3 Mem0 deferred).
-- Per CL-390 LOCKED: L0 fragments are cohort-keyed (NOT tenant-identifying);
-- k-anonymity enforces this. NO tenant_id column.
-- Per CL-28: k-anonymity threshold = 10 observations.
-- Per CL-417: canonical per-field-columns shape; no JSONB-blob payload.
--
-- write path: service-role through k-anonymity gate + PII reject (app layer).
-- read path: RLS policy enforces observation_count >= 10 at SQL layer
-- (defense-in-depth — app-layer query_l0 also respects threshold).

CREATE TABLE IF NOT EXISTS public.l0_fragments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fragment_type       TEXT NOT NULL
                        CHECK (fragment_type IN (
                            'routing_decision',
                            'specialist_outcome',
                            'trigger_pattern'
                        )),
    cohort_key          TEXT NOT NULL,
    content             JSONB NOT NULL,
    observation_count   INTEGER NOT NULL DEFAULT 1
                        CHECK (observation_count >= 1),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_observed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (fragment_type, cohort_key)
);

-- Hot-path read index: only fragments past k-anonymity threshold are queryable;
-- partial index keeps it small even as observation_count grows on rejected rows.
CREATE INDEX IF NOT EXISTS idx_l0_fragments_kanon
    ON public.l0_fragments (fragment_type, cohort_key)
    WHERE observation_count >= 10;

-- Recency index for ordering query_l0 results.
CREATE INDEX IF NOT EXISTS idx_l0_fragments_last_observed
    ON public.l0_fragments (last_observed_at DESC);

ALTER TABLE public.l0_fragments ENABLE ROW LEVEL SECURITY;

-- RLS: cross-tenant SELECT only when k-anonymity threshold reached. L0 is
-- cohort-keyed (CL-390) so there is no per-tenant restriction — every
-- subscriber learns from the same aggregated pool once k>=10.
-- Service-role bypasses RLS for the write path (k-anon gate enforced in
-- write_l0_fragment app layer + PII reject).
DROP POLICY IF EXISTS l0_fragments_kanon_select ON public.l0_fragments;
CREATE POLICY l0_fragments_kanon_select ON public.l0_fragments
    FOR SELECT TO PUBLIC
    USING (observation_count >= 10);

COMMENT ON TABLE public.l0_fragments IS
    'VT-126 L0 memory: cohort-keyed orchestrator-agent operational memory. Read via observability/l0_memory.query_l0 (RLS k>=10). Write via write_l0_fragment (service-role + PII reject + k-anon UPSERT).';
COMMENT ON COLUMN public.l0_fragments.cohort_key IS
    'Cohort identifier (e.g., "restaurant|tier_2|founding"). NEVER include tenant_id, phone, or any tenant-identifying value (CL-390).';
COMMENT ON COLUMN public.l0_fragments.observation_count IS
    'k-anonymity counter; cross-tenant aggregation. SELECT gated at >=10 by RLS policy l0_fragments_kanon_select.';
