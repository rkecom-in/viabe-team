-- 047_attribution_method_confidence.sql — VT-240 attribution provenance substrate.
--
-- Adds HOW each attribution was matched (attribution_method) + HOW confident
-- (attribution_confidence) to the attributions table (created in 023). These
-- are the fields VT-43 (get_attribution_data) degraded to None because there
-- was nowhere to read them from. VT-240 ships the COLUMNS + the
-- match_transactions output that feeds them; it does NOT build the attributions
-- WRITER (no writer exists yet — that's VT-176 / attribution-close, a separate
-- row) and does NOT lift VT-43 (which would read NULLs until a writer lands).
--
-- Both columns NULLABLE + additive: the table is empty on dev and any
-- pre-047-shape INSERT (method/confidence omitted) still succeeds. No RLS
-- change — 023 already ENABLEs + FORCEs RLS with four tenant policies, which
-- cover these columns automatically.
--
-- Migration number: 046 (VT-228 operator allowlist) is the prior highest. The
-- runner applies by sorted filename + tracks by name (schema_migrations.name),
-- so merge order relative to other 04x rows is irrelevant — 047 has no
-- dependency beyond 023 (attributions), which is long-merged.
--
-- Pillar 1 (revised 2026-05-12): attribution-close is deterministic; NO LLM
-- touches these rows. The method derives from a pure mapper
-- (attribution_method_from_match_basis) — reproducible, no float ambiguity in
-- the method choice (Fazal day-39 reproducibility gate).

ALTER TABLE attributions
    ADD COLUMN attribution_method TEXT NULL
        CHECK (attribution_method IN ('exact_match', 'window_match', 'manual_owner')),
    ADD COLUMN attribution_confidence REAL NULL
        CHECK (attribution_confidence IS NULL
               OR (attribution_confidence >= 0 AND attribution_confidence <= 1));

COMMENT ON COLUMN attributions.attribution_method IS
    'VT-240: how the payment was matched to a campaign. '
    'exact_match (VPA present in match_basis) / window_match (amount[/time] '
    'only) derive from match_transactions via attribution_method_from_match_basis; '
    'manual_owner is owner-asserted (set by a different path, not the matcher). '
    'NULL until a writer (VT-176) populates it.';
COMMENT ON COLUMN attributions.attribution_confidence IS
    'VT-240: the match composite score in [0,1] (TransactionMatch.confidence). '
    'NULL until a writer populates it.';
