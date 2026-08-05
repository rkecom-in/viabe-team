-- VT-733 slice B — the invisible half of tenant cost: everything that is NOT an LLM call.
--
-- Fazal 2026-08-05: "we can know how much is being consumed by the Manager, the specialists and any
-- other integrations we have." The LLM half has had a per-call ledger since mig 173. The other half
-- — Twilio messages, Voyage embeddings, Sarvam ASR seconds, Apify actor runs, ScrapingBee requests —
-- has never been metered at all, so every "what does this tenant cost us" number to date has been
-- an undercount of unknown size. Slice C's repricing brief is only as honest as this table.
--
-- Shape deliberately mirrors ``llm_call_events`` (mig 173) rather than inventing a second idiom:
-- one row per billable event, tenant-scoped, RLS-forced, cost computed at write time from a
-- config-driven rate. A reader who understands one table understands both.
--
-- ESTIMATED vs MEASURED is a first-class column, not a footnote. Some vendors bill per-unit-opaque
-- (an Apify actor run's compute units are not knowable at call time), so those rows record the units
-- we DO know plus our best-known rate, flagged ``is_estimated``. A repricing decision built on
-- estimates that look like measurements is exactly the failure this row exists to prevent — slice C
-- must be able to say "X measured, Y estimated" rather than one confident total.

CREATE TABLE IF NOT EXISTS integration_cost_events (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- NULL tenant = a platform-level cost with no single tenant to attribute it to (mirrors
    -- llm_call_events' own platform rows).
    tenant_id         uuid REFERENCES tenants(id) ON DELETE CASCADE,
    -- 'twilio' | 'voyage' | 'sarvam' | 'apify' | 'scrapingbee' — the VENDOR, not our module.
    vendor            text NOT NULL,
    -- What was billed: 'template_message' | 'session_message' | 'embedding' | 'asr_seconds' |
    -- 'actor_run' | 'request'. Vendor-specific by design; the rate table keys on (vendor, unit).
    unit              text NOT NULL,
    quantity          numeric NOT NULL DEFAULT 1,
    -- The rate actually applied, persisted with the row: a later rate change must never silently
    -- rewrite history (the same reason llm_call_events stores cost rather than recomputing it).
    unit_rate_usd     numeric NOT NULL DEFAULT 0,
    cost_usd          numeric NOT NULL DEFAULT 0,
    -- TRUE when the quantity or the rate is our best estimate rather than a vendor-reported fact.
    is_estimated      boolean NOT NULL DEFAULT false,
    -- Which of our surfaces incurred it, so cost attributes to the Manager vs a specialist the same
    -- way llm_call_events.agent does.
    agent             text,
    call_site         text,
    -- Vendor-side identifier (a Twilio SID, an Apify run id) for reconciliation against the invoice.
    external_ref      text,
    occurred_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS integration_cost_events_tenant_time_idx
    ON integration_cost_events (tenant_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS integration_cost_events_vendor_time_idx
    ON integration_cost_events (vendor, occurred_at DESC);

ALTER TABLE integration_cost_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_cost_events FORCE ROW LEVEL SECURITY;

-- Tenant-scoped read under the app GUC, matching the house RLS idiom. Platform rows (tenant_id
-- NULL) are readable only by a BYPASSRLS/service connection — they belong to no tenant.
DROP POLICY IF EXISTS integration_cost_events_tenant_read ON integration_cost_events;
CREATE POLICY integration_cost_events_tenant_read ON integration_cost_events
    FOR SELECT
    USING (tenant_id::text = current_setting('app.tenant_id', true));

-- No write policy: writes go through the privileged service connection (the metering seam), exactly
-- like llm_call_events. And no INSERT/UPDATE/DELETE grants to anon/authenticated — the loaded-gun
-- class VT-733A had to revoke on the caps tables is simply never created here.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT SELECT, INSERT ON integration_cost_events TO service_role;
    END IF;
END $$;

COMMENT ON TABLE integration_cost_events IS
    'VT-733B: one row per NON-LLM billable event (Twilio/Voyage/Sarvam/Apify/ScrapingBee). Mirrors '
    'llm_call_events. is_estimated distinguishes vendor-reported facts from our best guess — slice '
    'C''s repricing brief must report the two separately, never as one confident total.';
