-- 207_vt742_sender_pin_and_live_waba_uniqueness.sql — VT-742 §1 + §2.
--
-- Two things, both in service of ONE resolver (`integrations/sender_resolution.resolve_sender`)
-- answering "which number does this tenant send from".
--
-- ## 1. tenants.pinned_sender_e164 — PIN, never round-robin
--
-- VT-742 §2, decided BEFORE a second sending number exists, because it is a column now and a
-- data migration later. When the estate grows, each tenant is assigned ONE shared number and
-- stays on it: round-robin would spread a single bad tenant's reputation damage across every
-- number we own, whereas pinning contains it to the tenants sharing that one number.
--
-- Today the estate is one number, so the pin is NULL everywhere and precedence step 2 never
-- fires in production. It is built now so exit gate (g) holds — the design must be provable
-- with one number, not require buying a second to be testable.
--
-- Deliberately NOT unique: many tenants pin to the same shared number. That is the whole idea.
-- The CHECK is the same E.164 shape the transport asserts (VT-487) and `utils/phone_e164` owns,
-- so a malformed pin cannot be stored at all — the resolver's runtime ignore-and-log is the
-- second line, not the first.
--
-- NOT to be confused with `tenants.whatsapp_number`, which is the OWNER-inbound identity key
-- (mig 066 partial UNIQUE / VT-267) and the owner-notification recipient. Migration 050's comment
-- calling that column "the WABA sender" is STALE; nothing in src/ writes it. The customer-facing
-- sender identity lives in `tenant_whatsapp_accounts.phone_number` and now, for shared numbers,
-- here.
--
-- ## 2. One live WABA number belongs to at most one tenant
--
-- Customer inbound resolves the tenant by the number the customer messaged TO:
--     SELECT tenant_id FROM tenant_whatsapp_accounts WHERE phone_number = %s AND status='live' LIMIT 1
-- Migration 069 created no unique index on `phone_number`, so two tenants CAN claim the same live
-- number, and that `LIMIT 1` would then silently route one tenant's customer replies to the other.
-- Nothing today violates it (dev 2026-08-17: 134 live rows, 134 distinct numbers, 0 duplicate
-- groups) — which is exactly when an invariant is cheap to make structural.
--
-- Pre-check before applying to a new environment (this index will FAIL LOUDLY on a violation,
-- which is correct for a cross-tenant routing key — do not weaken it, fix the data):
--     SELECT phone_number FROM public.tenant_whatsapp_accounts
--      WHERE status='live' AND phone_number IS NOT NULL
--      GROUP BY 1 HAVING count(*) > 1;
--
-- Partial on status='live' only: a superseded/parked row keeping a historical number is fine, and
-- the routing query only ever reads live rows.

ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS pinned_sender_e164 TEXT;

ALTER TABLE public.tenants
    DROP CONSTRAINT IF EXISTS tenants_pinned_sender_e164_check;

ALTER TABLE public.tenants
    ADD CONSTRAINT tenants_pinned_sender_e164_check
    CHECK (pinned_sender_e164 IS NULL OR pinned_sender_e164 ~ '^\+[1-9][0-9]{7,14}$');

COMMENT ON COLUMN public.tenants.pinned_sender_e164 IS
    'VT-742 §2: the SHARED sending number this tenant is pinned to, plain E.164, NULL = use the '
    'default shared sender. Precedence step 2 in integrations/sender_resolution.resolve_sender '
    '(own live WABA > pin > default). Pinned, never round-robin, so one bad tenant''s reputation '
    'damage is contained to the tenants sharing its number. NOT the owner-inbound identity key — '
    'that is tenants.whatsapp_number.';

CREATE UNIQUE INDEX IF NOT EXISTS tenant_whatsapp_accounts_live_phone_key
    ON public.tenant_whatsapp_accounts (phone_number)
    WHERE status = 'live' AND phone_number IS NOT NULL;

COMMENT ON INDEX public.tenant_whatsapp_accounts_live_phone_key IS
    'VT-742: a live WABA number identifies exactly ONE tenant. api/twilio_ingress '
    '_lookup_customer_inbound_tenant resolves a customer inbound by this number with LIMIT 1, so a '
    'shared or duplicated value would route one tenant''s customer replies to another.';
