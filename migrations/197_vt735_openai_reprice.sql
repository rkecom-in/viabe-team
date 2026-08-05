-- 197 — VT-735: reprice the OpenAI family. The registry was 5× stale on our busiest model.
--
-- SOURCE: https://developers.openai.com/api/docs/pricing — verified 2026-08-06 (Clau, ratified into
-- `.viabe/model-tier-policy.md` and the VT-735 row, which carries the full table). OpenAI cut prices
-- and migration 173's seed never followed.
--
-- WHY THIS IS NOT COSMETIC
-- Dev runs ENTIRELY on gpt-5.6-luna — VT-732's boot-conformance line prints all five tiers as
-- gpt-5.6-luna — so every luna cost the VT-733 console has reported was **5× too high**. Fazal asked
-- for that console "on priority, so that we can know how much is being consumed"; a console that
-- overstates the bill fivefold answers his question wrongly, and the repricing brief would have
-- inherited the error. Fazal flagged the discrepancy; the sheet confirms he was right.
--
-- VERIFIED FIGURES (USD per 1M tokens, SHORT context; long-context is 2× these per the docs):
--     gpt-5.6-luna   standard 0.20 / 1.20   (was 1.00 / 6.00  -> 5×    stale)
--     gpt-5.6-terra  standard 2.00 / 12.00  (was 2.50 / 15.00 -> 1.25× stale)
--     gpt-5.6-sol    standard 5.00 / 30.00  (already correct — left untouched, stated so the
--                                            absence of a row here reads as verified, not skipped)
--
-- Flex/batch remain 0.5× via the existing `discount_multiplier`, which reproduces the sheet exactly
-- (luna flex 0.10/0.60). Cached-in stays 0.1× (luna cached-in 0.02 against 0.20 input) — also exact.
--
-- FAST TIER — deliberately NOT a column here. The sheet prices `fast` at 2× standard (luna
-- 0.40/2.40). That is a PREMIUM and must not ride `discount_multiplier`, which is a per-MODEL
-- discount for the batch lane: folding the two together would bill a Fast call at HALF price on any
-- flex-discounted model, under-reporting exactly the tier the policy spends most per token on. It
-- lives as `_PREMIUM_TIER_MULTIPLIERS` in `llm/pricing.py` instead, because the 2× is a uniform
-- published rate rather than a per-model figure — adding a `fast_multiplier` column now would ship
-- schema that nothing reads, and a knob nothing reads is worse than no knob. When a model prices its
-- fast lane differently, that is the moment the column earns its place.

BEGIN;

UPDATE model_pricing
   SET usd_per_mtok_in  = 0.2000,
       usd_per_mtok_out = 1.2000,
       updated_by       = 'vt735-reprice-197',
       updated_at       = now()
 WHERE model = 'gpt-5.6-luna';

UPDATE model_pricing
   SET usd_per_mtok_in  = 2.0000,
       usd_per_mtok_out = 12.0000,
       updated_by       = 'vt735-reprice-197',
       updated_at       = now()
 WHERE model = 'gpt-5.6-terra';

COMMIT;
