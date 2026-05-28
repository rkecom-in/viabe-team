-- 036_twilio_inbound_events_cascade.sql — VT-200 hygiene fix 2.
--
-- twilio_inbound_events.tenant_id currently FK's to tenants(id) with NO
-- ACTION (default). DSR-purge tests had to delete-order the child rows
-- explicitly to avoid ForeignKeyViolation. ON DELETE CASCADE makes the
-- parent-row delete cascade through.
--
-- Constraint name verified live: ``twilio_inbound_events_tenant_id_fkey``.
-- Drop + re-add inside a single migration so the table stays referentially
-- consistent across the swap.

ALTER TABLE public.twilio_inbound_events
    DROP CONSTRAINT IF EXISTS twilio_inbound_events_tenant_id_fkey;

ALTER TABLE public.twilio_inbound_events
    ADD CONSTRAINT twilio_inbound_events_tenant_id_fkey
    FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)
    ON DELETE CASCADE;

COMMENT ON CONSTRAINT twilio_inbound_events_tenant_id_fkey
    ON public.twilio_inbound_events IS
    'VT-200: ON DELETE CASCADE so DSR-purge of a tenant cleans children automatically.';
