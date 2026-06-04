-- 094_vt311_l2_retention.sql — VT-311 L2 episodic retention (18-month soft-delete).
--
-- VT-66 deferred L2 retention out of #275. This adds a `deleted_at` soft-delete
-- marker to episodic_events: the nightly VT-311 sweep stamps `deleted_at` on rows
-- older than the configured window (default 18 months / 548 days), and the L2
-- read path (l2_query.recent_events / events_for_entity / count_events) excludes
-- soft-deleted rows. The ROW stays (audit + chain integrity); it just leaves the
-- agent's working context — DPDP storage-limitation without destroying history.
--
-- NOTE: soft-delete (retention) is ORTHOGONAL to VT-76 reconstitution
-- (referenced_entity_id -> sentinel on opt-out). reconstitute_customer's direct
-- UPDATE has no deleted_at filter, so an opt-out still scrubs the customer link
-- on already-soft-deleted rows.
--
-- The partial index keeps the read path fast at scale (the 100K-event perf test):
-- it indexes ONLY live rows, so recent_events never scans retention-expired ones.
-- mig number claimed via scripts/migration_id_allocate.py; 093 was taken by VT-320
-- on a sibling branch (allocate-once discipline, CL-424) so this is 094.

ALTER TABLE public.episodic_events
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_episodic_events_live_recent
    ON public.episodic_events (tenant_id, occurred_at DESC)
    WHERE deleted_at IS NULL;
