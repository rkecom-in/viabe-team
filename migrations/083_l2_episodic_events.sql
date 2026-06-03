-- 083_l2_episodic_events.sql — VT-66 L2 episodic memory schema.
--
-- L2 = the agent's time-ordered "what happened recently in this tenant" log
-- (distinct from L1's "what is true now"). Append-only; templated summaries
-- (NOT LLM); structured payloads, NO raw PII (CL-390).
--
-- Idempotency (Cowork req 1 — dual-projection exactly-once): event_id links an
-- episodic row to the kg_events outbox event that produced it; the partial
-- UNIQUE makes the L2 projection a no-op on re-drain (the L1 projection is
-- already idempotent via l1_entities.external_key + kg_events_processed). The
-- drain marks kg_events.drained_at only after BOTH projections succeed, so a
-- crash between them re-drains to exactly-once in each.
--
-- VT-76 reconstitution hook: referenced_entity_id/type is the anonymization
-- target a future opt-out sweep nulls→sentinel. Tenant-scoped RLS (Pillar 3).
-- Claimed via scripts/migration_id_allocate.py (CL-424).

CREATE TABLE episodic_events (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              UUID NOT NULL REFERENCES tenants (id),
    -- the outbox event that produced this row (dual-projection idempotency);
    -- NULL for any future direct (non-outbox) record_episodic_event call.
    event_id               UUID,
    event_type             TEXT NOT NULL CHECK (event_type IN (
        'campaign_proposed', 'campaign_approved', 'campaign_rejected',
        'campaign_sent', 'attribution_closed',
        'customer_dormant_threshold_crossed', 'customer_high_value_threshold_crossed',
        'owner_message_received', 'agent_dispatch_completed',
        'agent_dispatch_terminated', 'phase_transitioned', 'clarification_resolved'
    )),
    summary                TEXT,
    payload                JSONB NOT NULL DEFAULT '{}'::jsonb,
    referenced_entity_type TEXT,
    referenced_entity_id   UUID,  -- VT-76 reconstitution anonymization target
    occurred_at            TIMESTAMPTZ NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE episodic_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE episodic_events FORCE ROW LEVEL SECURITY;

CREATE POLICY episodic_events_select ON episodic_events FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY episodic_events_insert ON episodic_events FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY episodic_events_update ON episodic_events FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY episodic_events_delete ON episodic_events FOR DELETE
    USING (tenant_id = app_current_tenant());

-- Dual-projection idempotency: one episodic row per outbox event per tenant.
CREATE UNIQUE INDEX episodic_events_event_id_uniq
    ON episodic_events (tenant_id, event_id)
    WHERE event_id IS NOT NULL;

-- The agent's primary read pattern: recent events for a tenant.
CREATE INDEX episodic_events_tenant_time
    ON episodic_events (tenant_id, occurred_at DESC);

-- VT-76 reconstitution sweep target lookup.
CREATE INDEX episodic_events_referenced_entity
    ON episodic_events (tenant_id, referenced_entity_id)
    WHERE referenced_entity_id IS NOT NULL;
