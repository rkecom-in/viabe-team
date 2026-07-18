-- 177_llm_cached_tokens.sql — prompt-caching observability (cache batch, 2026-07-18):
-- add the CACHE-READ input-token count to the per-call LLM audit ledger.
--
--   * ``llm_call_events.cached_tokens_in`` — the cache-read subset of this call's input
--     tokens (Anthropic ``cache_read_input_tokens`` / langchain ``input_token_details.cache_read``).
--     The existing ``tokens_in`` stays the TOTAL input (uncached + cached) for audit
--     continuity; this column makes the split queryable so cache-hit-rate dashboards
--     (cached_tokens_in vs tokens_in, per agent / call_site / model) read straight off
--     the ledger (Anthropic email 2026-07-18 — lands alongside the cache_control fixes
--     on the SR executor + onboarding turn-brain system prompts). ``cost_usd`` already
--     prices the cached portion at ``cached_in_multiplier`` (migration 173) — nothing
--     recomputes. Cache-unaware writers that omit the column get DEFAULT 0
--     (== "no cache read recorded"), unchanged behavior.

ALTER TABLE llm_call_events
    ADD COLUMN cached_tokens_in BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN llm_call_events.cached_tokens_in IS
    'Cache-read input tokens for this call (the cached subset of tokens_in, which stays '
    'the TOTAL input, uncached + cached). Enables cache-hit-rate queries '
    '(cached_tokens_in vs tokens_in per agent/call_site/model) — Anthropic email '
    '2026-07-18. Already priced at cached_in_multiplier inside cost_usd at write time '
    '(migration 173); 0 = no cache read recorded.';
