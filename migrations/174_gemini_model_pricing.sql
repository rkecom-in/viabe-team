-- 174_gemini_model_pricing.sql — Gemini 3.5 / 3.1 support (Fazal 2026-07-13).
-- Prices VERIFIED 2026-07-13 from ai.google.dev/gemini-api/docs/pricing. Same multipliers as 173:
-- cache reads 0.1x input; batch AND flex = 50% (discount_multiplier 0.5) on all three.
-- gemini-3.1-pro-preview is tier-priced by context (>200k = $4/$18); recorded at the <=200k rate —
-- our production contexts stay under 200k (active window is token-budgeted); revisit if that changes.
-- gemini-3.1-pro-preview is a PREVIEW model (Google may change/retire it) — VTR-tunable like all rows.
INSERT INTO model_pricing (model, usd_per_mtok_in, usd_per_mtok_out, updated_by) VALUES
    ('gemini-3.5-flash',        1.5000,  9.0000, 'seed-174'),
    ('gemini-3.1-flash-lite',   0.2500,  1.5000, 'seed-174'),
    ('gemini-3.1-pro-preview',  2.0000, 12.0000, 'seed-174-le200k-rate')
ON CONFLICT (model) DO NOTHING;
