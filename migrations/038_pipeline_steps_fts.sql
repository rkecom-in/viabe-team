-- VT-216 — pipeline_steps free-text search at scale
--
-- Replaces the VT-201 PR-2 ILIKE fallback with a proper PostgreSQL FTS
-- column + GIN index. Single tsvector spans BOTH input_envelope and
-- output_envelope so the operator history-view free-text query keeps
-- exact result-set parity with the prior OR-of-ILIKE behavior.
--
-- english analyzer per Phase-1 scope (multi-lingual = own row).
-- GENERATED ALWAYS STORED populates existing rows on the ALTER and
-- every future INSERT — no separate backfill job.
--
-- Idempotency: ALTER TABLE ADD COLUMN does not support IF NOT EXISTS
-- on generated columns, so the DO-block swallows duplicate_column on
-- re-apply (mirrors other migrations in this repo). The GIN index
-- uses IF NOT EXISTS natively.

DO $$
BEGIN
    ALTER TABLE public.pipeline_steps
        ADD COLUMN envelope_search_tsv TSVECTOR
        GENERATED ALWAYS AS (
            to_tsvector(
                'english',
                coalesce(input_envelope::text, '')
                    || ' '
                    || coalesce(output_envelope::text, '')
            )
        ) STORED;
EXCEPTION WHEN duplicate_column THEN
    NULL;
END $$;

CREATE INDEX IF NOT EXISTS pipeline_steps_envelope_search_tsv_gin
    ON public.pipeline_steps USING GIN (envelope_search_tsv);
