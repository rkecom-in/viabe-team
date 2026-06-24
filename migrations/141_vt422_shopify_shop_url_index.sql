-- 141_vt422_shopify_shop_url_index.sql — VT-422 GAP-2: Shopify webhook tenant resolution.
--
-- The public Shopify OAuth app delivers app-registered webhooks. Unlike the
-- retired sheet-push path, Shopify does NOT inject a custom tenant header — the
-- ONLY tenant linkage on an app-delivered webhook is the ``X-Shopify-Shop-Domain``
-- header. The reworked handler (api/shopify_webhook.py, VT-422 GAP-2) resolves the
-- tenant via:
--
--     SELECT tenant_id FROM tenant_oauth_tokens
--      WHERE connector_id = 'shopify' AND shop_url = <X-Shopify-Shop-Domain>
--
-- That lookup runs on EVERY inbound webhook (orders/create, orders/paid, …), so it
-- needs an index on ``shop_url``. Today the only index on tenant_oauth_tokens is the
-- composite PK (tenant_id, connector_id) — a shop_url lookup falls back to a seq
-- scan. Add a partial index over the Shopify rows keyed on (shop_url, connector_id).
--
-- ``shop_url`` (the column) was added by 035_shopify_substrate.sql:15 (ADD COLUMN IF
-- NOT EXISTS), so it exists at runtime even though it is not in 033's DDL — this
-- index targets that existing column.
--
-- Non-unique: in principle a shop could be re-installed under a different tenant
-- (the handler rejects an ambiguous/multi-row resolution), so we do NOT enforce
-- uniqueness here — the index is for lookup speed, the ambiguity guard is in code.

CREATE INDEX IF NOT EXISTS tenant_oauth_tokens_shopify_shop_url
    ON public.tenant_oauth_tokens (shop_url, connector_id)
    WHERE connector_id = 'shopify';

COMMENT ON INDEX public.tenant_oauth_tokens_shopify_shop_url IS
    'VT-422 GAP-2: speeds the Shopify webhook tenant resolution (shop_url -> tenant_id) '
    'on every app-delivered webhook. Partial over connector_id=shopify rows.';
