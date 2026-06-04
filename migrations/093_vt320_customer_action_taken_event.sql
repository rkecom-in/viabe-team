-- 093_vt320_customer_action_taken_event.sql — VT-320 agent-action customer marker.
--
-- VT-320 emits a customer-referencing episodic row (referenced_entity_type=
-- 'customer') when the agent ACTS on a specific customer (a campaign send) — so
-- VT-76's reconstitution sweep has real rows to anonymize on opt-out (otherwise
-- a forever no-op). That needs a clean episodic event_type: `customer_action_taken`
-- (D2=(b), Cowork 20260604T111500Z — NOT overloading VT-312's repurposed
-- `*_threshold_crossed` names, which read as detector/brain-decides events; this
-- is a privacy-critical taxonomy driving reconstitution, so semantics matter).
--
-- Per CL-428 the DB CHECK must stay in exact sync with the l2_types Literal —
-- this migration + the l2_types.L2EventType edit ship in the SAME PR. Re-creates
-- the inline CHECK to the full set (12 existing + customer_action_taken = 13).
-- Claimed via scripts/migration_id_allocate.py (CL-424).

ALTER TABLE public.episodic_events
    DROP CONSTRAINT IF EXISTS episodic_events_event_type_check;

ALTER TABLE public.episodic_events
    ADD CONSTRAINT episodic_events_event_type_check CHECK (event_type IN (
        -- mig 083 originals
        'campaign_proposed',
        'campaign_approved',
        'campaign_rejected',
        'campaign_sent',
        'attribution_closed',
        'customer_dormant_threshold_crossed',
        'customer_high_value_threshold_crossed',
        'owner_message_received',
        'agent_dispatch_completed',
        'agent_dispatch_terminated',
        'phase_transitioned',
        'clarification_resolved',
        -- VT-320 agent-action customer marker
        'customer_action_taken'
    ));
