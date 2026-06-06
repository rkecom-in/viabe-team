-- 107_vt97_waitlist_signups.sql — pre-launch waitlist interest capture (VT-97).
-- Pre-tenant: collected on the public landing in `waitlist` launch mode, BEFORE any
-- tenant exists. Purpose-limited (CL-390): the sole use is to message the entrant when
-- Viabe Team launches; `notified_at` is set by the launch sweep, after which notified
-- rows are purged. consent_at records the DPDP collection consent (VT-97 #1).
--
-- NOT tenant-scoped → NOT covered by the tenant DSR `_PURGE_ORDER` (which purges by
-- tenant_id). Its erasure path is its OWN: the DELETE /api/waitlist ops endpoint +
-- the post-notify / retention-bound purge. Documented in docs/policy/waitlist-data.md
-- (VT-97 #2) so this PII table can't slip the purge.
CREATE TABLE waitlist_signups (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT NOT NULL,
    whatsapp_e164   TEXT NOT NULL,
    referral_source TEXT,
    consent_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    notified_at     TIMESTAMPTZ,
    -- Dedup: one waitlist row per email and per number (a re-submit is idempotent).
    CONSTRAINT waitlist_signups_email_uniq UNIQUE (email),
    CONSTRAINT waitlist_signups_whatsapp_uniq UNIQUE (whatsapp_e164)
);

-- Service-role-only (like razorpay_webhook_events): there is NO tenant context at
-- waitlist-capture time. RLS forced deny-all so no tenant-scoped connection can touch
-- it; the Supabase secret key / Postgres superuser bypasses RLS — the sole access path.
ALTER TABLE waitlist_signups ENABLE ROW LEVEL SECURITY;
ALTER TABLE waitlist_signups FORCE ROW LEVEL SECURITY;

CREATE POLICY waitlist_signups_no_tenant_access ON waitlist_signups
    FOR ALL
    USING (false)
    WITH CHECK (false);
