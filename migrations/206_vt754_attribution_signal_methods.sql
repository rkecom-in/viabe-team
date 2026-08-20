-- 206_vt754_attribution_signal_methods.sql — VT-754 / ruling D-C.
--
-- D-C (Fazal 2026-08-15, CL-2026-08-15-three-m2b-rulings): **attribution errs UNDER, never over.**
-- Only two things count as attribution — a tracked link/code, or a reply followed by a purchase
-- inside a defined window. Nothing else.
--
-- `attribution_method`'s CHECK admitted ('exact_match', 'window_match', 'manual_owner'). None of
-- those can name what D-C requires:
--
--   * `window_match` means "this recipient paid inside the window", which is EXACTLY the inference
--     the ruling rejects — a shop's sales continue whether or not we messaged anyone, so crediting a
--     coincident sale claims revenue the campaign did not cause. Writing the new, stricter matches
--     under that name would make an honest number indistinguishable from the over-claiming one it
--     replaces, in an OWNER-FACING figure.
--   * `exact_match` is the VT-240 UPI corroboration (VPA + amount to a specific outreach), a
--     different and stronger claim.
--
-- So the two ruled signals get their own names. The value IS the provenance: a reader can tell what
-- earned the credit without joining anything.
--
-- Rows already written as 'window_match' are LEFT ALONE and keep their name. They were produced by
-- the old predicate and re-labelling them would assert a signal that was never checked; the honest
-- record is that they are a different, weaker claim.

ALTER TABLE public.attributions
    DROP CONSTRAINT IF EXISTS attributions_attribution_method_check;

ALTER TABLE public.attributions
    ADD CONSTRAINT attributions_attribution_method_check
    CHECK (attribution_method IN (
        'exact_match',          -- VT-240: UPI VPA + amount corroboration to a specific outreach
        'window_match',         -- LEGACY, no longer written: recipient paid inside the window
        'manual_owner',         -- the owner said so
        'tracked_link',         -- D-C: the recipient clicked a tracked link, then purchased
        'reply_then_purchase'   -- D-C: the recipient replied to us, then purchased, inside the window
    ));

COMMENT ON COLUMN public.attributions.attribution_method IS
    'How the credit was earned. VT-754/D-C: tracked_link and reply_then_purchase are the only two '
    'signals that may be written for campaign attribution; window_match is legacy (a coincident '
    'sale is not attribution) and is retained only so existing rows keep an honest label.';
