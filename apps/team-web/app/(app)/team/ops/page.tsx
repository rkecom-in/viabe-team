/** Ops Console — workspace overview (VT-123 view 1 of 3). */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { HomeTriage } from '@/components/ops/home-triage'
import { fetchHomeTriage, type HomeTriageData } from '@/lib/ops/home'
import {
  fetchInFlightRuns,
  fetchTopTenants,
  fetchWorkspaceCounters,
} from '@/lib/ops/data-access'

export const dynamic = 'force-dynamic'

type CountersT = Awaited<ReturnType<typeof fetchWorkspaceCounters>>
type TopTenantsT = Awaited<ReturnType<typeof fetchTopTenants>>
type InFlightT = Awaited<ReturnType<typeof fetchInFlightRuns>>

export default async function OpsWorkspacePage() {
  // VT-290: requireOpsOperator wraps requireFazal + adds role + assigned-tenant scoping.
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops')
    throw err
  }

  // VT-290 Home/Triage (urgency-first, scoped + de-identified). Degrades to null on error.
  let homeTriage: HomeTriageData | null = null
  try {
    homeTriage = await fetchHomeTriage(operator)
  } catch (err) {
    console.error('OpsWorkspacePage: fetchHomeTriage failed', err)
  }

  // VT-217: per-fetch try/catch so one failing query degrades its
  // section instead of 500-ing the whole page (matches /ops/stream
  // pattern from VT-201 PR-3 afeb9d0).
  let counters: CountersT | null = null
  let countersError: string | null = null
  try {
    counters = await fetchWorkspaceCounters()
  } catch (err) {
    countersError = err instanceof Error ? err.message : 'unknown error'
    console.error('OpsWorkspacePage: fetchWorkspaceCounters failed', err)
  }

  let topTenants: TopTenantsT = []
  let topTenantsError: string | null = null
  try {
    topTenants = await fetchTopTenants(10)
  } catch (err) {
    topTenantsError = err instanceof Error ? err.message : 'unknown error'
    console.error('OpsWorkspacePage: fetchTopTenants failed', err)
  }

  let inFlight: InFlightT = []
  let inFlightError: string | null = null
  try {
    inFlight = await fetchInFlightRuns(20)
  } catch (err) {
    inFlightError = err instanceof Error ? err.message : 'unknown error'
    console.error('OpsWorkspacePage: fetchInFlightRuns failed', err)
  }

  return (
    <main
      className="ops-workspace bg-background min-h-screen p-6 space-y-6"
      data-area="team-ops-workspace"
    >
      <header>
        <h1 className="text-2xl font-semibold text-foreground">
          Ops Console — Workspace
        </h1>
      </header>

      {/* VT-290 Home/Triage — urgency-first, role-scoped + de-identified. */}
      {homeTriage && <HomeTriage data={homeTriage} />}

      <section
        data-section="counters"
        className="bg-card rounded-lg shadow-sm border border-border p-6"
      >
        <h2 className="text-lg font-medium text-foreground mb-4">Today</h2>
        {counters ? (
          <dl className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <div className="p-4 bg-muted/40 rounded">
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                In-flight runs
              </dt>
              <dd
                data-counter="in_flight_runs"
                className="text-3xl font-bold text-foreground mt-1"
              >
                {counters.in_flight_runs}
              </dd>
            </div>
            <div className="p-4 bg-muted/40 rounded">
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Total runs today
              </dt>
              <dd
                data-counter="total_runs_today"
                className="text-3xl font-bold text-foreground mt-1"
              >
                {counters.total_runs_today}
              </dd>
            </div>
            <div className="p-4 bg-muted/40 rounded">
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Escalations today
              </dt>
              <dd
                data-counter="escalations_today"
                className="text-3xl font-bold text-foreground mt-1"
              >
                {counters.escalations_today}
              </dd>
            </div>
            <div className="p-4 bg-muted/40 rounded">
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Cost burn today (paise)
              </dt>
              <dd
                data-counter="cost_burn_today_paise"
                className="text-3xl font-bold text-foreground mt-1"
              >
                {counters.cost_burn_today_paise}
              </dd>
            </div>
          </dl>
        ) : (
          <p
            data-section-error
            className="text-sm text-destructive bg-destructive/10 border border-destructive/30 rounded p-3"
          >
            couldn&apos;t load: {countersError}
          </p>
        )}
      </section>

      <section
        data-section="top-tenants"
        className="bg-card rounded-lg shadow-sm border border-border p-6"
      >
        <h2 className="text-lg font-medium text-foreground mb-4">
          Top tenants by activity
        </h2>
        {topTenantsError ? (
          <p
            data-section-error
            className="text-sm text-destructive bg-destructive/10 border border-destructive/30 rounded p-3"
          >
            couldn&apos;t load: {topTenantsError}
          </p>
        ) : (
          <table className="min-w-full divide-y divide-border">
            <thead className="bg-muted/40">
              <tr>
                <th className="px-4 py-2 text-xs font-medium uppercase text-muted-foreground text-left">
                  Tenant
                </th>
                <th className="px-4 py-2 text-xs font-medium uppercase text-muted-foreground text-left">
                  Runs
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {topTenants.map((t) => (
                <tr key={t.tenant_id} className="hover:bg-muted/40 transition-colors">
                  <td className="px-4 py-3 text-sm">
                    <a
                      href={`/team/ops/tenants/${t.tenant_id}`}
                      className="text-primary hover:underline font-mono text-xs"
                    >
                      {t.business_name ?? t.tenant_id}
                    </a>
                  </td>
                  <td className="px-4 py-3 text-sm text-foreground">{t.runs_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section
        data-section="in-flight"
        className="bg-card rounded-lg shadow-sm border border-border p-6"
      >
        <h2 className="text-lg font-medium text-foreground mb-4">
          In-flight runs
        </h2>
        {inFlightError ? (
          <p
            data-section-error
            className="text-sm text-destructive bg-destructive/10 border border-destructive/30 rounded p-3"
          >
            couldn&apos;t load: {inFlightError}
          </p>
        ) : (
          <table className="min-w-full divide-y divide-border">
            <thead className="bg-muted/40">
              <tr>
                <th className="px-4 py-2 text-xs font-medium uppercase text-muted-foreground text-left">
                  Run
                </th>
                <th className="px-4 py-2 text-xs font-medium uppercase text-muted-foreground text-left">
                  Tenant
                </th>
                <th className="px-4 py-2 text-xs font-medium uppercase text-muted-foreground text-left">
                  Started
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {inFlight.map((r) => (
                <tr key={r.run_id} className="hover:bg-muted/40 transition-colors">
                  <td className="px-4 py-3">
                    <a
                      href={`/team/ops/runs/${r.run_id}`}
                      className="font-mono text-xs text-primary hover:underline"
                    >
                      {r.run_id}
                    </a>
                  </td>
                  <td className="px-4 py-3">
                    <a
                      href={`/team/ops/tenants/${r.tenant_id}`}
                      className="font-mono text-xs text-primary hover:underline"
                    >
                      {r.tenant_id}
                    </a>
                  </td>
                  <td className="px-4 py-3 text-sm text-muted-foreground">
                    {new Date(r.started_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  )
}
