-- 063_upi_vpa_resolutions.sql — VT-57 UPI counterparty identity (VPA → customer).
--
-- UPI counterparties are identified by a VPA (UPI handle, e.g. asha@oksbi), NOT a
-- phone — but customers keys on phone_e164/email. Per the VT-46 ruling (UPI VPA
-- precision must be UPI-SCOPED, NOT bolted onto customer_ledger_entries/customers),
-- the VPA→customer link lives in this dedicated table.
--
-- D1 (Cowork VT-57 plan ruling 2026-06-01): GRANT app_role + RLS so this reads via
-- tenant_connection cleanly — explicitly NOT the phone_token_resolutions
-- owner-pool+explicit-WHERE hack (that grant fix is a separate Clau item). app_role
-- DML comes from the migration-015 default privileges (table created after 015),
-- same as 061/062.
--
-- Pillar 3: RLS in the same migration, FORCE (CL-82/88). CL-417 per-field columns.
-- CL-422 dev = synthetic only. Migration number 063 via the allocator (CL-424).
--
-- Resolution recording: when VT-57 resolves a VPA to a customer (either an exact
-- prior link here, or a `<phone>@upi` VPA that dedup_and_merge resolves), the link
-- is recorded here so the NEXT import of the same VPA is an O(1) exact hit.

CREATE TABLE IF NOT EXISTS public.upi_vpa_resolutions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL,
    vpa         TEXT NOT NULL,
    customer_id UUID NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- same-tenant composite FK: cross-tenant linkage physically impossible.
    CONSTRAINT upi_vpa_resolutions_customer_fk
        FOREIGN KEY (tenant_id, customer_id)
        REFERENCES public.customers (tenant_id, id) ON DELETE CASCADE,
    -- one customer per VPA per tenant; re-resolution is idempotent.
    CONSTRAINT upi_vpa_resolutions_idem UNIQUE (tenant_id, vpa)
);

CREATE INDEX IF NOT EXISTS idx_upi_vpa_resolutions_tenant_customer
    ON public.upi_vpa_resolutions (tenant_id, customer_id);

ALTER TABLE public.upi_vpa_resolutions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.upi_vpa_resolutions FORCE ROW LEVEL SECURITY;

CREATE POLICY upi_vpa_resolutions_select ON public.upi_vpa_resolutions
    FOR SELECT USING (tenant_id = app_current_tenant());
CREATE POLICY upi_vpa_resolutions_insert ON public.upi_vpa_resolutions
    FOR INSERT WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY upi_vpa_resolutions_update ON public.upi_vpa_resolutions
    FOR UPDATE USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY upi_vpa_resolutions_delete ON public.upi_vpa_resolutions
    FOR DELETE USING (tenant_id = app_current_tenant());
