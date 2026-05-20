-- 018_campaigns_v1.sql — reconcile campaigns to CampaignPlan v1.0 (VT-122 / VT-37).
--
-- Drops the v0.1 single-subscriber columns and replaces them with a single
-- plan_json JSONB column carrying the full v1.0 CampaignPlan dict. The
-- richer v1.0 shape (target_cohort.customer_ids list, message_plan, expected_arrr,
-- evidence_refs, exclusion_list, escalation_conditions) lives inside plan_json
-- instead of being denormalised into columns — Pillar 8, schema lives in one
-- place; queries that need to peek into the plan go through JSONB operators.
--
-- The `status` column stays as the lifecycle-progression column (initial
-- value 'proposed', flipped to approved/rejected/sent/failed downstream by
-- VT-6 / VT-5). Only the `proposed` agent-terminal variant writes a campaigns
-- row; the `out_of_scope` and `insufficient_data` refusal variants do NOT
-- (they don't produce a campaign — no row, no JSONB).
--
-- Pre-launch context: the `campaigns` table is empty on viabe-team-dev (no
-- production rows yet) so this is a clean schema reshape, not a data
-- migration. The ALTERs below assume an empty table; running them against
-- a populated table without a backfill would lose data.
ALTER TABLE campaigns DROP COLUMN subscriber_id;
ALTER TABLE campaigns DROP COLUMN template_id;
ALTER TABLE campaigns DROP COLUMN body_params;
ALTER TABLE campaigns DROP COLUMN proposed_by;
ALTER TABLE campaigns RENAME COLUMN proposed_at TO generated_at;
ALTER TABLE campaigns ADD COLUMN plan_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE campaigns ALTER COLUMN plan_json DROP DEFAULT;
