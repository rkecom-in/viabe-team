-- VT-768 / VT-769 PROPOSAL ONLY — deliberately not a numbered migration and NEVER run by Codex.
-- CC must allocate the migration number. In that SAME implementation change, CC must add
-- tenant_draft_artifacts to orchestrator.dsr_purge._PURGE_ORDER. Do not apply only the SQL half:
-- a DSR anonymizes tenants instead of deleting them, so the FK cascade is not the erasure path.

CREATE TABLE IF NOT EXISTS tenant_draft_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    artifact_key TEXT NOT NULL,
    artifact_kind TEXT NOT NULL CHECK (artifact_kind IN ('content_draft', 'campaign_proposal')),
    version INTEGER NOT NULL CHECK (version > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    parent_artifact_id UUID NULL REFERENCES tenant_draft_artifacts(id) ON DELETE SET NULL,
    parent_version INTEGER NULL CHECK (parent_version IS NULL OR parent_version > 0),
    created_by_agent TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at TIMESTAMPTZ NULL,
    CHECK ((parent_artifact_id IS NULL) = (parent_version IS NULL)),
    UNIQUE (tenant_id, artifact_key, version)
);

-- Recipient, customer-contact, delivery-state and transport columns are intentionally absent.
ALTER TABLE tenant_draft_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_draft_artifacts FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_draft_artifacts_tenant_isolation
ON tenant_draft_artifacts
USING (tenant_id = app_current_tenant_id())
WITH CHECK (tenant_id = app_current_tenant_id());

-- Required companion edit in the same CC-owned migration PR:
--   orchestrator/dsr_purge.py _PURGE_ORDER: add "tenant_draft_artifacts"
-- Required hard-delete canary: seed two tenants, purge one, assert physical COUNT(*) = 0 for the
-- purged tenant and the co-resident tenant survives. Never infer erasure from the FK.
