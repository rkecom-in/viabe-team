-- 074_ops_audit.sql — VT-292 operator-action audit log (append-only).
--
-- PURE OPS actions (resolve-status / override / reassign) are audited HERE, separate from
-- the VT-188 `privacy_audit_log` which stays for PII-REVEAL actions only (Cowork answer #3:
-- don't pollute the PII-access log with non-PII ops events). This is ALSO the substrate
-- VT-294 (Behaviour & Training — "measure VTR decision quality") consumes, so it's built
-- once here. Allocated alongside 073 up front (CL-424).
--
-- Append-only: no UPDATE/DELETE in the app path (the regulator/quality-analysis trail).
-- Per-field columns (CL-417), no JSONB. Deny-all FORCE RLS — service-role only (operator
-- path has no tenant GUC). CL-422 synthetic. Migration 074 via the allocator.

CREATE TABLE IF NOT EXISTS public.ops_audit (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id  UUID NOT NULL,                  -- who acted (VTR / VTAdmin)
    tenant_id    UUID NULL,                       -- the affected business, if scoped
    action       TEXT NOT NULL,                   -- resolve | override | reassign | ack | ...
    target_kind  TEXT NOT NULL,                   -- escalation | assignment | agent | ...
    target_id    TEXT NULL,                       -- the affected row id (escalation id, etc.)
    detail       TEXT NULL,                       -- short human note (no PII — CL-390)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ops_audit_operator
    ON public.ops_audit (operator_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_audit_tenant
    ON public.ops_audit (tenant_id, created_at DESC) WHERE tenant_id IS NOT NULL;

ALTER TABLE public.ops_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ops_audit FORCE ROW LEVEL SECURITY;
