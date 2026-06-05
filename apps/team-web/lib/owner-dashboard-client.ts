/**
 * VT-87 — team-web → orchestrator owner-dashboard read client.
 *
 * Mirrors `lib/owner-verify-client.ts` (INTERNAL_API_SECRET-signed). The orchestrator owns
 * the tenant-scoped reads + PII masking; the response carries MASKED data ONLY (phone
 * last-4 — raw phone never crosses this boundary, CL-390). team-web passes the SESSION-
 * derived tenantId (never a client-supplied field — the IDOR boundary).
 */

const _ORCHESTRATOR_DEFAULT = 'http://localhost:8001'
const _TIMEOUT_MS = 10_000

function _base(): string {
  return process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
}

function _secret(): string {
  return process.env.INTERNAL_API_SECRET ?? ''
}

export interface TopCustomer {
  display_name: string | null
  phone_last4: string | null // MASKED at source — never a raw phone
  spend_rupees: number
}

export interface RecentCampaign {
  campaign_id: string
  status: string | null
  template_id: string | null
  responses: number
  sent_at: string | null
}

export interface DashboardSummary {
  customer_count: number
  top_customers: TopCustomer[]
  recent_campaigns: RecentCampaign[]
}

export async function fetchDashboardSummary(
  tenantId: string,
): Promise<DashboardSummary | null> {
  try {
    const url = `${_base()}/api/orchestrator/owner/dashboard-summary?tenant_id=${encodeURIComponent(
      tenantId,
    )}`
    const res = await fetch(url, {
      method: 'GET',
      headers: { 'X-Internal-Secret': _secret() },
      signal: AbortSignal.timeout(_TIMEOUT_MS),
      cache: 'no-store',
    })
    if (!res.ok) return null
    return (await res.json()) as DashboardSummary
  } catch {
    return null
  }
}
