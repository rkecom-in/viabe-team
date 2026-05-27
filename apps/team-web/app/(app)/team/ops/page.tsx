/** Ops Console — workspace overview (VT-123 view 1 of 3). */

import { redirect } from 'next/navigation'

import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import {
  fetchInFlightRuns,
  fetchTopTenants,
  fetchWorkspaceCounters,
} from '@/lib/ops/data-access'

export const dynamic = 'force-dynamic'

export default async function OpsWorkspacePage() {
  try {
    await requireFazal()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/login')
    throw err
  }

  const [counters, topTenants, inFlight] = await Promise.all([
    fetchWorkspaceCounters(),
    fetchTopTenants(10),
    fetchInFlightRuns(20),
  ])

  return (
    <main className="ops-workspace" data-area="team-ops-workspace">
      <header>
        <h1>Ops Console — Workspace</h1>
      </header>

      <section data-section="counters">
        <h2>Today</h2>
        <dl>
          <div>
            <dt>In-flight runs</dt>
            <dd data-counter="in_flight_runs">{counters.in_flight_runs}</dd>
          </div>
          <div>
            <dt>Total runs today</dt>
            <dd data-counter="total_runs_today">{counters.total_runs_today}</dd>
          </div>
          <div>
            <dt>Escalations today</dt>
            <dd data-counter="escalations_today">{counters.escalations_today}</dd>
          </div>
          <div>
            <dt>Cost burn today (paise)</dt>
            <dd data-counter="cost_burn_today_paise">
              {counters.cost_burn_today_paise}
            </dd>
          </div>
        </dl>
      </section>

      <section data-section="top-tenants">
        <h2>Top tenants by activity</h2>
        <table>
          <thead>
            <tr>
              <th>Tenant</th>
              <th>Runs</th>
            </tr>
          </thead>
          <tbody>
            {topTenants.map((t) => (
              <tr key={t.tenant_id}>
                <td>
                  <a href={`/team/ops/tenants/${t.tenant_id}`}>
                    {t.business_name ?? t.tenant_id}
                  </a>
                </td>
                <td>{t.runs_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section data-section="in-flight">
        <h2>In-flight runs</h2>
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>Tenant</th>
              <th>Started</th>
            </tr>
          </thead>
          <tbody>
            {inFlight.map((r) => (
              <tr key={r.run_id}>
                <td>
                  <a href={`/team/ops/runs/${r.run_id}`}>{r.run_id}</a>
                </td>
                <td>
                  <a href={`/team/ops/tenants/${r.tenant_id}`}>{r.tenant_id}</a>
                </td>
                <td>{new Date(r.started_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  )
}
