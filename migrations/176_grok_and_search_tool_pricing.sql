-- 176_grok_and_search_tool_pricing.sql — xAI Grok models + web/X-search tool cost (Fazal 2026-07-13).
-- Prices VERIFIED 2026-07-13 from docs.x.ai/developers/pricing.
--
-- Grok (xAI) is OpenAI Responses-API compatible (base https://api.x.ai/v1, /responses) — same
-- ChatOpenAI path as gpt-5.6, base_url via XAI_BASE_URL (self-host/proxy switch), key XAI_API_KEY.
-- grok-4.5 = flagship (no batch tier -> discount 1.0); grok-4.3 = cheaper, 20% batch => discount 0.8
-- (NOT the 0.5 default). xAI bills cached tokens at standard rate -> cached_in_multiplier 1.0.
INSERT INTO model_pricing
    (model, usd_per_mtok_in, usd_per_mtok_out, cached_in_multiplier, discount_multiplier, updated_by)
VALUES
    ('grok-4.5', 2.0000, 6.0000, 1.000, 1.000, 'seed-176'),
    ('grok-4.3', 1.2500, 2.5000, 1.000, 0.800, 'seed-176')
ON CONFLICT (model) DO NOTHING;

-- Search-tool cost registry: server-side web/X search is billed PER INVOCATION, separate from
-- tokens (Fazal: "each LLM call must be recorded ... everything"). VTR-tunable, same read-only-to-
-- runtime posture as model_pricing. Seeded with VERIFIED numbers where public; PLACEHOLDER where the
-- provider publishes no clear per-search rate (OpenAI Responses web_search, Google grounding) —
-- VTR corrects via the console. (provider, tool) is the key.
CREATE TABLE search_tool_pricing (
    provider          TEXT NOT NULL,          -- anthropic | openai | google | xai | zai
    tool              TEXT NOT NULL,          -- web_search | x_search
    usd_per_1000      NUMERIC(10, 4) NOT NULL,
    updated_by        TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, tool)
);

ALTER TABLE search_tool_pricing ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_tool_pricing FORCE ROW LEVEL SECURITY;
CREATE POLICY search_tool_pricing_select ON search_tool_pricing FOR SELECT USING (true);
GRANT SELECT ON search_tool_pricing TO app_role;

INSERT INTO search_tool_pricing (provider, tool, usd_per_1000, updated_by) VALUES
    ('anthropic', 'web_search', 10.0000, 'seed-176'),          -- verified: $10/1000
    ('xai',       'web_search',  5.0000, 'seed-176'),          -- verified: $5/1000
    ('xai',       'x_search',    5.0000, 'seed-176'),          -- verified: $5/1000
    ('openai',    'web_search', 10.0000, 'seed-176-PLACEHOLDER'),
    ('google',    'web_search', 35.0000, 'seed-176-PLACEHOLDER');

-- The per-call ledger records search invocations + their cost alongside token cost. cost_usd stays
-- the TOKEN cost; search_cost_usd is additive so token-vs-search spend stays separately queryable.
ALTER TABLE llm_call_events
    ADD COLUMN search_count    INT NOT NULL DEFAULT 0,
    ADD COLUMN search_cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0;
