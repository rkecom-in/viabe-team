-- VT-723 — record whether a source SUPPORTS or REFUTES the claim it is attached to.
--
-- WHY
-- ---
-- `knowledge_card_sources` (mig 182) records that a source is evidence FOR a card, and nothing
-- more. Every edge is therefore implicitly corroborating. That is fine for a corpus built only
-- from agreeing sources, and wrong the moment an authoritative source CONTRADICTS a claim — which
-- is exactly the case PR #553 surfaced: a disputed T4 forum claim whose authoritative refutation
-- has to persist as a refutation, not silently as more corroboration.
--
-- Without this column that refutation is unrepresentable. The PR's own body claimed it persisted
-- with `supports=false`; the review found there was nowhere in the schema to record stance at all,
-- and the insert that tried raised `UndefinedColumn` on its first row.
--
-- WHY THIS IS THE DANGEROUS DIRECTION TO GET WRONG
-- ------------------------------------------------
-- A refuting source stored as a supporting one does not merely lose information — it INVERTS it.
-- The independence-cluster corroboration logic counts supporting clusters to promote a card's
-- confidence, so a strong refutation would COUNT TOWARD promoting the very claim it demolishes.
-- The card would end up better-corroborated the more authoritatively it was contradicted.
--
-- DEFAULT TRUE is therefore deliberate and safe: it preserves the meaning of all 104 pre-existing
-- edges, which were all written by paths that only ever attach agreeing evidence. New writers must
-- pass stance explicitly when it is not support.
--
-- Additive, non-breaking: existing readers that select no stance are unaffected.
--
-- NOT ADDED: `relevance`. The PR's insert also carried it, hardcoded to 1 and never read anywhere —
-- an unused column is a future misreading waiting to happen, so it is deliberately omitted rather
-- than shipped "for later".

ALTER TABLE public.knowledge_card_sources
    ADD COLUMN IF NOT EXISTS supports BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN public.knowledge_card_sources.supports IS
    'VT-723: does this source SUPPORT (true) or REFUTE (false) the card''s claim? Defaults true so '
    'every pre-existing edge keeps its original meaning. Corroboration counting MUST filter on '
    'supports = true — counting a refutation as corroboration would promote a claim in proportion '
    'to how authoritatively it was contradicted.';
