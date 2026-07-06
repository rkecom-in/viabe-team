-- 168_vt608_connector_field_mapping.sql — VT-608 fix round CRITICAL 2: persist the owner-
-- confirmed field mapping durably, keyed the SAME (tenant_id, connector_id) way the recurring-
-- pull scheduler already reads its config.
--
-- Finding: confirm_mapping persisted the confirmed {source_column: canonical_field} mapping only
-- into tenant_integration_state.pending_owner_input (031) — an EPHEMERAL envelope that gets
-- overwritten as the phase machine advances (e.g. execute_pending_ingestion_commit's own success
-- path replaces it with a fresh 'cadence_choice' envelope carrying no metadata at all). Both the
-- initial commit AND every subsequent recurring-pull sweep (integrations/scheduler.py) need the
-- SAME mapping to actually transform rows per the owner's confirmation (not silently fall back to
-- the alias-guess mapper) — that requires a home that OUTLIVES the onboarding phase transitions.
--
-- tenant_connector_status (034) is the natural fit: it is ALREADY the per-(tenant,connector)
-- durable operational-config row the scheduler reads every sweep. A single JSONB column here is a
-- deliberate, scoped exception to that table's own "no JSONB blob" comment (CL-19) — the shape is
-- a flat {source_column: canonical_field} string map (no PII, no row values, CL-104/390), and a
-- typed side-table for one flexible dict would be pure ceremony over this Phase-1 need. NULL means
-- "no confirmed mapping on file" — every sheet_row_to_canonical caller treats that identically to
-- omitting the mapping argument (falls back to the alias table, byte-identical to before this
-- migration).

ALTER TABLE public.tenant_connector_status
    ADD COLUMN IF NOT EXISTS field_mapping JSONB;

COMMENT ON COLUMN public.tenant_connector_status.field_mapping IS
    'VT-608 — owner-confirmed {source_column: canonical_field} mapping (confirm_mapping tool). NULL = no confirmed mapping; ingestion falls back to the alias-based mapper. No PII (column labels only).';
