-- 035_shopify_substrate.sql — VT-208 Shopify connector persistence.
--
-- Reuses ``tenant_oauth_tokens`` (033) as the credential store: per VT-208
-- Q1 lock, the ``refresh_token_encrypted`` column for ``connector_id =
-- 'shopify'`` rows holds the Admin API access_token (Shopify custom-app
-- tokens are long-lived; no OAuth refresh dance). Semantic note via
-- COMMENT below — same column, two valid interpretations gated on
-- connector_id.
--
-- New column: ``shop_url`` (e.g. ``rkecom.myshopify.com``). Nullable
-- because google_sheet rows don't need it. Per CL-19 typed columns
-- rather than JSONB blob for the per-connector metadata.

ALTER TABLE public.tenant_oauth_tokens
    ADD COLUMN IF NOT EXISTS shop_url TEXT;

COMMENT ON COLUMN public.tenant_oauth_tokens.shop_url IS
    'VT-208: Shopify shop hostname (e.g. <shop>.myshopify.com). NULL for non-Shopify connectors.';

COMMENT ON COLUMN public.tenant_oauth_tokens.refresh_token_encrypted IS
    'Encrypted credential. google_sheet (VT-207): OAuth refresh_token. shopify (VT-208): Admin API access_token (custom-app long-lived; no refresh).';
