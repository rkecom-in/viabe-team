-- 175_glm_model_pricing.sql — GLM-5.2 (Z.ai) support (Fazal 2026-07-13).
-- Price VERIFIED 2026-07-13 from docs.z.ai/guides/overview/pricing: $1.40 in / $4.40 out per MTok;
-- cached input $0.26 => per-model cached_in_multiplier 0.186 (NOT the 0.1 default). NO batch/flex
-- tier published => discount_multiplier 1.0 (a mistaken service_tier='batch' record must not
-- under-cost).
-- SELF-HOST NOTE (Fazal): GLM-5.2 is explicitly a self-host candidate (GLM_BASE_URL env points the
-- OpenAI-compatible client at any endpoint — z.ai default or a self-hosted vLLM/sglang domain).
-- When self-hosted, the true unit cost is infra amortization, not this API rate — VTR updates this
-- row (or zeroes it) at cutover; the ledger keeps recording tokens either way.
INSERT INTO model_pricing
    (model, usd_per_mtok_in, usd_per_mtok_out, cached_in_multiplier, discount_multiplier, updated_by)
VALUES
    ('glm-5.2', 1.4000, 4.4000, 0.186, 1.000, 'seed-175')
ON CONFLICT (model) DO NOTHING;
