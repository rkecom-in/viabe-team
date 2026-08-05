/**
 * VT-733 slice A — Ops Console COST view: what each tenant costs us, by agent and model.
 *
 * Fazal 2026-08-05: "important that we have this in place on priority, so that we can know how much
 * is being consumed by the Manager, the specialists and any other integrations we have."
 *
 * Gating mirrors the sibling ops pages: requireOpsOperator → redirect to ops login on
 * UnauthorizedError. Scoping is server-side and assignment-derived (scopeCostTenantFilter); the
 * operator never supplies which tenants they may see (IDOR rule, VT-293/294).
 *
 * Two deliberate honesty choices, because a cost console that flatters is worse than none:
 *   - A tenant with NO configured ceiling renders "no cap" in amber, never a comfortable 0% bar.
 *     As of 2026-08-05 that is EVERY tenant: global_llm_limits holds NULL day/month ceilings and
 *     tenant_llm_limits is empty, so the enforcement layer is switched on and configured to no
 *     limit. The console must show that rather than imply protection.
 *   - Spend is compared against the cap's OWN window (day cap vs today, month cap vs this month),
 *     never mixed, so "78% used" means what it says. The table shows MONTH utilisation, because
 *     `tenant_llm_limits` has no daily column at all — per-tenant ceilings are MONTHLY only, and the
 *     daily ceiling exists solely on `global_llm_limits`, platform-wide. Showing a per-tenant day
 *     figure would be a knob operators do not actually have.
 *
 * Styled inline to the VT-405/VT-412 ops design language (light-mode cards + chips), matching the
 * sibling tenants page rather than inventing a second visual vocabulary.
 */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { capUtilisation, hasFullCostAccess, scopeCostTenantFilter } from '@/lib/ops/cost-access'
import {
  fetchTenantCapStatus,
  fetchTenantCostSummary,
  type TenantCapStatusRow,
  type TenantCostRow,
} from '@/lib/ops/data-access'

export const dynamic = 'force-dynamic'

function usd(n: number): string {
  return `$${n.toFixed(n < 1 ? 4 : 2)}`
}

const CAP_TONE: Record<TenantCapStatusRow['cap_state'], { bg: string; fg: string; label: string }> = {
  // "no cap" is amber, not grey: it is a live exposure, not a neutral absence.
  none: { bg: '#fef3c7', fg: '#92400e', label: 'no cap set' },
  ok: { bg: '#dcfce7', fg: '#166534', label: 'within cap' },
  soft: { bg: '#fef3c7', fg: '#92400e', label: 'soft cap' },
  hard: { bg: '#fee2e2', fg: '#991b1b', label: 'HARD CAP' },
}

interface TenantRollup {
  tenantId: string
  businessName: string | null
  totalUsd: number
  byAgent: Map<string, number>
}

/** Fold the per-(tenant, agent, model) rows into one row per tenant, keeping the agent split —
 *  the breakdown Fazal asked for ("the Manager, the specialists and any other integrations"). */
function rollup(rows: TenantCostRow[]): TenantRollup[] {
  const byTenant = new Map<string, TenantRollup>()
  for (const r of rows) {
    let t = byTenant.get(r.tenant_id)
    if (!t) {
      t = {
        tenantId: r.tenant_id,
        businessName: r.business_name,
        totalUsd: 0,
        byAgent: new Map(),
      }
      byTenant.set(r.tenant_id, t)
    }
    const line = Number(r.cost_usd ?? 0) + Number(r.search_cost_usd ?? 0)
    t.totalUsd += line
    t.byAgent.set(r.agent, (t.byAgent.get(r.agent) ?? 0) + line)
  }
  return [...byTenant.values()].sort((a, b) => b.totalUsd - a.totalUsd)
}

export default async function OpsCostPage(): Promise<React.ReactElement> {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login')
    throw err
  }

  const { tenantIds } = scopeCostTenantFilter(operator.assignedTenants, undefined)
  const since = new Date()
  since.setUTCDate(1)
  since.setUTCHours(0, 0, 0, 0)

  const [costRows, caps] = await Promise.all([
    fetchTenantCostSummary(since, tenantIds),
    fetchTenantCapStatus(tenantIds),
  ])
  const tenants = rollup(costRows)
  const capsById = new Map(caps.map((c) => [c.tenant_id, c]))
  const platformTotal = tenants.reduce((acc, t) => acc + t.totalUsd, 0)
  const uncapped = caps.filter((c) => c.cap_state === 'none').length

  return (
    <main style={{ padding: '24px', maxWidth: 1100, margin: '0 auto' }}>
      <h1 style={{ fontSize: 22, fontWeight: 600, marginBottom: 4 }}>Tenant cost</h1>
      <p style={{ color: '#4b5563', fontSize: 13, marginBottom: 20 }}>
        LLM spend this month, by tenant and agent.{' '}
        {hasFullCostAccess(operator.assignedTenants)
          ? 'All tenants.'
          : 'Your assigned tenants only.'}
      </p>

      <section
        style={{
          display: 'flex',
          gap: 16,
          marginBottom: 24,
          padding: 16,
          background: '#f9fafb',
          border: '1px solid #e5e7eb',
          borderRadius: 8,
        }}
      >
        <div>
          <div style={{ fontSize: 11, color: '#6b7280', textTransform: 'uppercase' }}>
            Spend this month
          </div>
          <div style={{ fontSize: 20, fontWeight: 600 }}>{usd(platformTotal)}</div>
        </div>
        <div>
          <div style={{ fontSize: 11, color: '#6b7280', textTransform: 'uppercase' }}>Tenants</div>
          <div style={{ fontSize: 20, fontWeight: 600 }}>{tenants.length}</div>
        </div>
        <div>
          <div style={{ fontSize: 11, color: '#6b7280', textTransform: 'uppercase' }}>
            Without a cap
          </div>
          {/* Stated plainly: an uncapped tenant can spend without limit, and today that is all of them. */}
          <div style={{ fontSize: 20, fontWeight: 600, color: uncapped > 0 ? '#92400e' : '#166534' }}>
            {uncapped}
          </div>
        </div>
      </section>

      {tenants.length === 0 ? (
        <p style={{ color: '#6b7280', fontSize: 14 }}>No metered spend in this window.</p>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ textAlign: 'left', borderBottom: '1px solid #e5e7eb' }}>
              <th style={{ padding: '8px 6px' }}>Tenant</th>
              <th style={{ padding: '8px 6px' }}>Month</th>
              <th style={{ padding: '8px 6px' }}>By agent</th>
              <th style={{ padding: '8px 6px' }}>Cap</th>
            </tr>
          </thead>
          <tbody>
            {tenants.map((t) => {
              const cap = capsById.get(t.tenantId)
              const tone = CAP_TONE[cap?.cap_state ?? 'none']
              const util = cap ? capUtilisation(cap.spend_month_usd, cap.max_cost_usd_month) : null
              const agents = [...t.byAgent.entries()].sort((a, b) => b[1] - a[1])
              return (
                <tr key={t.tenantId} style={{ borderBottom: '1px solid #f3f4f6' }}>
                  <td style={{ padding: '10px 6px' }}>
                    {t.businessName ?? t.tenantId.slice(0, 8)}
                  </td>
                  <td style={{ padding: '10px 6px', fontVariantNumeric: 'tabular-nums' }}>
                    {usd(t.totalUsd)}
                  </td>
                  <td style={{ padding: '10px 6px', color: '#4b5563' }}>
                    {agents.map(([agent, v]) => `${agent} ${usd(v)}`).join(' · ')}
                  </td>
                  <td style={{ padding: '10px 6px' }}>
                    <span
                      style={{
                        background: tone.bg,
                        color: tone.fg,
                        padding: '2px 8px',
                        borderRadius: 999,
                        fontSize: 11,
                        fontWeight: 600,
                      }}
                    >
                      {tone.label}
                    </span>
                    {util !== null && (
                      <span style={{ marginLeft: 8, color: '#6b7280' }}>
                        {(util * 100).toFixed(0)}% of month cap
                      </span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </main>
  )
}
