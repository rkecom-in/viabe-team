-- 180_vt691_whatsapp_signup_sessions.sql — VT-691: WhatsApp-initiated signup, pre-tenant
-- consent state.
--
-- WHY (Fazal 2026-07-21, prod smoke insight): for a WhatsApp-first product the natural front
-- door IS the inbound WhatsApp. An unknown_sender inbound becomes a signup — but a tenant is
-- created ONLY after an explicit consent reply (DPDP; the reply is the capture the public
-- page's checkboxes are). Between the consent PROMPT and the consent REPLY there is no tenant,
-- so the pending state needs its own pre-tenant table.
--
-- PRIVACY POSTURE (the waitlist_signups precedent, mig 107 + docs/policy/waitlist-data.md):
-- raw phone_e164 stored pre-tenant, gated behind FORCE RLS deny-all (service-role/BYPASSRLS
-- only — no tenant context exists to scope by). No tenant_id at insert => NOT covered by the
-- tenant DSR purge order; hygiene is the module's own retention sweep (expired/stale sessions
-- deleted after WHATSAPP_SIGNUP_RETENTION_DAYS, opportunistic at prompt time). A session that
-- converts stamps tenant_id for audit and is then inert (status='consented').
--
-- ABUSE GATE: one row per phone (UNIQUE) makes repeated inbounds idempotent — they bump
-- prompt bookkeeping on the SAME row, never spawn duplicates. consent_prompt_count +
-- last_prompt_at carry the per-number cooldown (max prompts / min interval enforced in code).

CREATE TABLE whatsapp_signup_sessions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_e164           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'consent_pending'
                             CHECK (status IN ('consent_pending', 'consented', 'declined', 'expired')),
    consent_prompt_count INT NOT NULL DEFAULT 1,
    last_prompt_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    consented_at         TIMESTAMPTZ NULL,
    -- stamped on conversion (audit link); the tenant row + consent_records are the authority.
    tenant_id            UUID NULL REFERENCES tenants (id) ON DELETE SET NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT whatsapp_signup_sessions_phone_uniq UNIQUE (phone_e164)
);

ALTER TABLE whatsapp_signup_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE whatsapp_signup_sessions FORCE ROW LEVEL SECURITY;

-- Deny-all: pre-tenant PII, service-role only (the waitlist_signups idiom).
CREATE POLICY whatsapp_signup_sessions_deny_all ON whatsapp_signup_sessions
    USING (false) WITH CHECK (false);

COMMENT ON TABLE whatsapp_signup_sessions IS
    'VT-691 — pre-tenant consent state for WhatsApp-initiated signup. Raw phone under FORCE-RLS '
    'deny-all (service-role only; waitlist precedent). Tenant created ONLY on an explicit '
    'consent reply; module-owned retention purge (no tenant_id => outside tenant DSR purge).';
