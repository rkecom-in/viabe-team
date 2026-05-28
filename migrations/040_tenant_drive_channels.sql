-- VT-222 — Drive Push channels per (tenant, sheet)
--
-- Backs the customer-correct Sheet integration flow per CL-421
-- (zero-manual-paste). One row per active Google Drive push channel.
-- Webhook handler verifies X-Goog-Channel-Token against this table
-- before any DB write. Channel renewal scheduler renews rows
-- approaching expiry (Google max = 7 days).
--
-- channel_token is a verify-only secret, not a credential; stored in
-- plaintext (Fernet wrap not needed per VT-222 review note).

CREATE TABLE IF NOT EXISTS public.tenant_drive_channels (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    connector_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    channel_id TEXT NOT NULL UNIQUE,
    channel_token TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_notification_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tenant_drive_channels_expires
    ON public.tenant_drive_channels (expires_at);

CREATE INDEX IF NOT EXISTS idx_tenant_drive_channels_tenant_connector
    ON public.tenant_drive_channels (tenant_id, connector_id);

ALTER TABLE public.tenant_drive_channels ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_drive_channels_tenant_isolation ON public.tenant_drive_channels;
CREATE POLICY tenant_drive_channels_tenant_isolation
    ON public.tenant_drive_channels
    USING (tenant_id = app_current_tenant());
