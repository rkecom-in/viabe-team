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

export interface Customer {
  display_name: string | null
  phone_last4: string | null // MASKED at source — never a raw phone
  opt_out_status: string | null
  spend_rupees: number
}

export interface CustomersPage {
  page: number
  page_size: number
  total: number
  customers: Customer[]
}

export async function fetchCustomers(
  tenantId: string,
  opts: { page?: number; pageSize?: number; excludedOnly?: boolean } = {},
): Promise<CustomersPage | null> {
  const { page = 1, pageSize = 20, excludedOnly = false } = opts
  try {
    const url =
      `${_base()}/api/orchestrator/owner/dashboard-customers` +
      `?tenant_id=${encodeURIComponent(tenantId)}` +
      `&page=${page}&page_size=${pageSize}&excluded_only=${excludedOnly}`
    const res = await fetch(url, {
      method: 'GET',
      headers: { 'X-Internal-Secret': _secret() },
      signal: AbortSignal.timeout(_TIMEOUT_MS),
      cache: 'no-store',
    })
    if (!res.ok) return null
    return (await res.json()) as CustomersPage
  } catch {
    return null
  }
}

export interface CampaignsList {
  campaigns: RecentCampaign[]
}

export async function fetchCampaigns(
  tenantId: string,
  opts: { daysBack?: number; limit?: number } = {},
): Promise<CampaignsList | null> {
  const { daysBack = 365, limit = 50 } = opts
  try {
    const url =
      `${_base()}/api/orchestrator/owner/dashboard-campaigns` +
      `?tenant_id=${encodeURIComponent(tenantId)}&days_back=${daysBack}&limit=${limit}`
    const res = await fetch(url, {
      method: 'GET',
      headers: { 'X-Internal-Secret': _secret() },
      signal: AbortSignal.timeout(_TIMEOUT_MS),
      cache: 'no-store',
    })
    if (!res.ok) return null
    return (await res.json()) as CampaignsList
  } catch {
    return null
  }
}

export interface OwnerSettings {
  business: {
    business_name: string | null
    business_archetype: string | null
    owner_name: string | null
    locale: string | null
    working_hours: string | null
  } | null
  plan: {
    plan_tier: string | null
    phase: string | null
    trial_started_at: string | null
    trial_ends_at: string | null
    preferred_language: string | null
  }
}

export async function fetchSettings(tenantId: string): Promise<OwnerSettings | null> {
  try {
    const url = `${_base()}/api/orchestrator/owner/dashboard-settings?tenant_id=${encodeURIComponent(
      tenantId,
    )}`
    const res = await fetch(url, {
      method: 'GET',
      headers: { 'X-Internal-Secret': _secret() },
      signal: AbortSignal.timeout(_TIMEOUT_MS),
      cache: 'no-store',
    })
    if (!res.ok) return null
    return (await res.json()) as OwnerSettings
  } catch {
    return null
  }
}

export interface ReportItem {
  year_month: string
  generated_at: string | null
  has_pdf: boolean
}

export interface ReportsList {
  reports: ReportItem[]
}

export async function fetchReports(tenantId: string): Promise<ReportsList | null> {
  try {
    const url = `${_base()}/api/orchestrator/owner/dashboard-reports?tenant_id=${encodeURIComponent(
      tenantId,
    )}`
    const res = await fetch(url, {
      method: 'GET',
      headers: { 'X-Internal-Secret': _secret() },
      signal: AbortSignal.timeout(_TIMEOUT_MS),
      cache: 'no-store',
    })
    if (!res.ok) return null
    return (await res.json()) as ReportsList
  } catch {
    return null
  }
}
