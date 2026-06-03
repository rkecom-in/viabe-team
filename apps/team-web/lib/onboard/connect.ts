/**
 * VT-267 PR-C — owner wizard connect/handoff + connection-status resume (server-side).
 *
 * OAuth inside the WhatsApp in-app WebView is blocked by Google AND Meta
 * (`disallowed_useragent`, blocked popups). Fazal ruling 2026-06-03: the wizard renders in
 * the WebView, but the OAuth connect steps HAND OFF to the system browser, then the owner
 * returns to the wizard which RE-CHECKS connection status to resume.
 *
 * - `startConnect(tenantId, connector)` → asks the orchestrator (server-side, INTERNAL_API_SECRET)
 *   to mint a VT-289 nonce + return the provider authorize URL. The wizard opens this in the
 *   SYSTEM browser (full-page external navigation; never a WebView popup).
 * - `checkConnection(tenantId, connector)` → reads the connection's true status from the
 *   service-role substrate (tenant_connector_status for Sheets, tenant_whatsapp_accounts for
 *   WhatsApp). This is the RESUME signal — no client cache, resolved per request.
 *
 * Both connectors' setup endpoints already exist (VT-207 google_sheet, VT-286 whatsapp,
 * both VT-289-hardened). This module is the team-web orchestration + status read; it never
 * sees a token or a phone number (CL-390).
 */

import { serverSecretClient } from '@/lib/supabase-client'

export type WizardConnector = 'google_sheet' | 'whatsapp'

const _ORCHESTRATOR_DEFAULT = 'http://localhost:8001'
const _SETUP_TIMEOUT_MS = 10_000

const _SETUP_PATH: Record<WizardConnector, string> = {
  google_sheet: '/api/orchestrator/integrations/google_sheet/setup',
  whatsapp: '/api/orchestrator/integrations/whatsapp/setup',
}
// The provider authorize URL is returned under different keys per connector.
const _URL_KEY: Record<WizardConnector, string> = {
  google_sheet: 'auth_url',
  whatsapp: 'embedded_signup_url',
}

export interface StartConnectResult {
  ok: boolean
  /** The provider authorize URL to open in the SYSTEM browser (null on failure). */
  authUrl: string | null
  /** ok | http_<n> | timeout | error | misconfig */
  reason: string
}

/** Start an OAuth connect: orchestrator mints the VT-289 nonce + returns the authorize URL.
 *  The wizard opens the URL in the system browser (handoff). Never throws. */
export async function startConnect(
  tenantId: string,
  connector: WizardConnector,
): Promise<StartConnectResult> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
  const secret = process.env.INTERNAL_API_SECRET ?? ''
  if (!tenantId) return { ok: false, authUrl: null, reason: 'misconfig' }
  try {
    const res = await fetch(`${base}${_SETUP_PATH[connector]}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'X-Internal-Secret': secret },
      body: JSON.stringify({ tenant_id: tenantId }),
      signal: AbortSignal.timeout(_SETUP_TIMEOUT_MS),
    })
    if (!res.ok) return { ok: false, authUrl: null, reason: `http_${res.status}` }
    const data = (await res.json()) as Record<string, unknown>
    const url = data[_URL_KEY[connector]]
    if (typeof url !== 'string' || !url) return { ok: false, authUrl: null, reason: 'error' }
    return { ok: true, authUrl: url, reason: 'ok' }
  } catch (err) {
    const timedOut = err instanceof Error && err.name === 'TimeoutError'
    return { ok: false, authUrl: null, reason: timedOut ? 'timeout' : 'error' }
  }
}

export interface ConnectionStatus {
  connector: WizardConnector
  /** True iff the connection is complete enough to proceed (the resume gate). */
  connected: boolean
  /** Raw status detail for display (no PII). */
  detail: string
}

/** Read the true connection status server-side (the resume signal after the browser handoff).
 *  Fail-CLOSED: any error → not connected (the wizard keeps the owner on the connect step). */
export async function checkConnection(
  tenantId: string,
  connector: WizardConnector,
  client: { from: (t: string) => any } = serverSecretClient(),
): Promise<ConnectionStatus> {
  if (!tenantId) return { connector, connected: false, detail: 'no tenant' }
  try {
    if (connector === 'google_sheet') {
      const { data, error } = await client
        .from('tenant_connector_status')
        .select('enabled, last_status')
        .eq('tenant_id', tenantId)
        .eq('connector_id', 'google_sheet')
        .maybeSingle()
      if (error || !data) return { connector, connected: false, detail: 'not connected' }
      const row = data as { enabled: boolean; last_status: string | null }
      // Connected once the connector row exists + is enabled (OAuth completed → row written).
      return {
        connector,
        connected: Boolean(row.enabled),
        detail: row.last_status ?? (row.enabled ? 'connected' : 'disabled'),
      }
    }
    // whatsapp: tenant_whatsapp_accounts.status (pending→verifying→name_approved→live).
    const { data, error } = await client
      .from('tenant_whatsapp_accounts')
      .select('status')
      .eq('tenant_id', tenantId)
      .maybeSingle()
    if (error || !data) return { connector, connected: false, detail: 'not connected' }
    const status = (data as { status: string }).status
    // "Connected" for the wizard = the WABA exists past pending (signup completed). Sends still
    // gated on `live` downstream (wa_send_allowed); the wizard only needs signup-complete.
    return { connector, connected: status !== 'pending', detail: status }
  } catch {
    return { connector, connected: false, detail: 'error' }
  }
}
