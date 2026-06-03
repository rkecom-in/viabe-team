-- 082_kg_events_outbox.sql — VT-65 PR-2: transactional outbox for KG events.
--
-- emit_kg_event() INSERTs a row here using the CALLER's connection, INSIDE the
-- source write's transaction — so the event is atomic with the source write
-- (commits together; rolls back together). A drain reads undrained rows and
-- applies them via the idempotent process_kg_event consumer (kg_events_processed),
-- then stamps drained_at. Immediate best-effort drain runs post-commit at each
-- site; the scheduled sweep (VT-307) is the reliability backstop.
--
-- Tenant-scoped RLS (Pillar 3). Claimed via scripts/migration_id_allocate.py (CL-424).

CREATE TABLE kg_events (
    event_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type  TEXT NOT NULL,
    tenant_id   UUID NOT NULL REFERENCES tenants (id),
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    emitted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    drained_at  TIMESTAMPTZ
);

ALTER TABLE kg_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE kg_events FORCE ROW LEVEL SECURITY;

CREATE POLICY kg_events_select ON kg_events FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY kg_events_insert ON kg_events FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY kg_events_update ON kg_events FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

-- Drain sweep: undrained rows, oldest first.
CREATE INDEX kg_events_undrained_idx
    ON kg_events (tenant_id, emitted_at)
    WHERE drained_at IS NULL;
