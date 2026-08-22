-- S2 REVIEW PROPOSAL ONLY — NOT A MIGRATION, NOT ALLOCATED, NOT RUN.
-- CC must allocate the migration number and reconcile exact shared-column conventions before land.
-- The same landed change must add all three tenant tables to dsr_purge._PURGE_ORDER and must prove
-- physical zero rows after DSR. RLS + FORCE RLS are inseparable from table creation.

BEGIN;

CREATE TABLE commerce_consent_grants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    phone_token text NOT NULL CHECK (phone_token LIKE 'phone_tok_%'),
    channel text NOT NULL CHECK (channel IN ('whatsapp')),
    purpose text NOT NULL CHECK (
        purpose IN ('checkout_recovery', 'replenishment_reminder', 'cod_order_confirmation')
    ),
    state text NOT NULL CHECK (state IN ('active', 'withdrawn')),
    notice_version text NOT NULL,
    locale text NOT NULL CHECK (locale IN ('en', 'hi', 'hinglish')),
    capture_surface text NOT NULL,
    source_system text NOT NULL,
    source_reference text NOT NULL,
    affirmative_at timestamptz NOT NULL,
    withdrawn_at timestamptz,
    evidence_hash text NOT NULL CHECK (length(evidence_hash) = 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, phone_token, channel, purpose),
    CHECK (
        (state = 'active' AND withdrawn_at IS NULL)
        OR (state = 'withdrawn' AND withdrawn_at IS NOT NULL)
    )
);

CREATE TABLE commerce_consent_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    grant_id uuid NOT NULL REFERENCES commerce_consent_grants(id),
    phone_token text NOT NULL CHECK (phone_token LIKE 'phone_tok_%'),
    channel text NOT NULL CHECK (channel IN ('whatsapp')),
    purpose text NOT NULL CHECK (
        purpose IN ('checkout_recovery', 'replenishment_reminder', 'cod_order_confirmation')
    ),
    event_type text NOT NULL CHECK (event_type IN ('grant', 'reaffirm', 'withdraw')),
    actor_type text NOT NULL CHECK (
        actor_type IN ('customer', 'global_stop', 'system_replay', 'vtr')
    ),
    actor_reference text NOT NULL,
    reason text NOT NULL,
    notice_version text,
    occurred_at timestamptz NOT NULL,
    evidence_hash text NOT NULL CHECK (length(evidence_hash) = 64),
    idempotency_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE checkout_recovery_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    source_kind text NOT NULL CHECK (source_kind IN ('shopify', 'reports_funnel')),
    source_attempt_id text NOT NULL,
    attempt_version text NOT NULL,
    contact_token text NOT NULL CHECK (contact_token LIKE 'phone_tok_%'),
    created_at_source timestamptz NOT NULL,
    updated_at_source timestamptz NOT NULL,
    completed_at_source timestamptz,
    total_paise bigint NOT NULL CHECK (total_paise >= 0),
    currency text NOT NULL CHECK (currency = 'INR'),
    item_count integer NOT NULL CHECK (item_count > 0),
    destination_reference_encrypted text NOT NULL,
    evidence_reference text NOT NULL,
    terminal boolean NOT NULL DEFAULT false,
    quarantine_reason text,
    draft_batch_id uuid REFERENCES agent_draft_batches(id),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source_kind, source_attempt_id, attempt_version),
    CHECK (updated_at_source >= created_at_source),
    CHECK (completed_at_source IS NULL OR completed_at_source >= created_at_source),
    CHECK (NOT terminal OR completed_at_source IS NOT NULL OR quarantine_reason IS NOT NULL)
);

ALTER TABLE commerce_consent_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce_consent_grants FORCE ROW LEVEL SECURITY;
ALTER TABLE commerce_consent_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce_consent_events FORCE ROW LEVEL SECURITY;
ALTER TABLE checkout_recovery_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkout_recovery_attempts FORCE ROW LEVEL SECURITY;

CREATE POLICY commerce_consent_grants_tenant_isolation ON commerce_consent_grants
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY commerce_consent_events_tenant_isolation ON commerce_consent_events
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY checkout_recovery_attempts_tenant_isolation ON checkout_recovery_attempts
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE OR REPLACE FUNCTION s2_proposal_consent_events_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'commerce_consent_events is append-only';
END;
$$;

CREATE TRIGGER commerce_consent_events_no_update
BEFORE UPDATE ON commerce_consent_events
FOR EACH ROW EXECUTE FUNCTION s2_proposal_consent_events_append_only();

CREATE TRIGGER commerce_consent_events_no_delete
BEFORE DELETE ON commerce_consent_events
FOR EACH ROW EXECUTE FUNCTION s2_proposal_consent_events_append_only();

-- The application operations are deliberately named transactions, not generic upserts:
--   grant_commerce_consent       -> INSERT current row + INSERT grant event
--   withdraw_commerce_consent    -> UPDATE exact active purpose + INSERT withdraw event
--   reaffirm_commerce_consent    -> INSERT affirmative event, then reactivate exact purpose
--   withdraw_all_on_global_stop  -> withdraw every active purpose + one event per purpose
-- No ON CONFLICT branch may set withdrawn_at = NULL.
--
-- DSR requirement for the allocated migration/code change:
--   dsr_purge._PURGE_ORDER must delete checkout_recovery_attempts before consent events before
--   consent grants, and the hard-delete canary must count zero physical rows after purge.

ROLLBACK;
