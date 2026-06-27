/**
 * VT-412 — Ops Console tenants INDEX (the browse-list the audit found missing).
 *
 * Operators previously reached tenants/[tenantId] only via deep-links — there was no "browse my
 * assigned tenants" entry point. This is it: a scoped, de-identified table, each row linking to
 * the existing tenants/[tenantId] view (VT-405).
 *
 * Gating mirrors the sibling ops pages: requireOpsOperator (JWT + allowlist + role) → redirect to
 * ops login on UnauthorizedError. Scoping is server-side, assignment-derived (fetchAssignedTenants
 * reads operator.assignedTenants — VTR → assigned only, fail-closed; VTAdmin → all); the operator
 * never supplies which tenants they may see (IDOR rule, VT-293/294).
 *
 * Styled inline to the VT-405 tenants-page design language (light-mode cards + status chips) so the
 * page stands alone on dev — the shared components/ops/ops-ui.tsx (PR-A) is NOT on origin/dev yet.
 */

import Link from 'next/link'
import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { fetchAssignedTenants, type TenantIndexRow } from '@/lib/ops/tenants-index'

export const dynamic = 'force-dynamic'

function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString()
}

const VERIFICATION_TONE: Record<string, 'gray' | 'green' | 'amber'> = {
  unverified: 'gray',
  gstin_verified: 'amber',
  vtr_verified: 'green',
}

const VERIFICATION_LABEL: Record<string, string> = {
  unverified: 'unverified',
  gstin_verified: 'GSTIN verified',
  vtr_verified: 'VTR verified',
}

function Chip({ children, tone }: { children: React.ReactNode; tone: 'gray' | 'blue' | 'green' | 'amber' }) {
  const tones: Record<string, string> = {
    gray: 'bg-muted text-muted-foreground border-border',
    blue: 'bg-primary/10 text-primary border-primary/30',
    green: 'bg-secondary/10 text-secondary border-secondary/30',
    amber: 'bg-gold/15 text-gold-foreground border-gold/40',
  }
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${tones[tone]}`}>
      {children}
    </span>
  )
}

function VerificationChip({ status }: { status: string | null }) {
  if (!status) return <Chip tone="gray">—</Chip>
  return <Chip tone={VERIFICATION_TONE[status] ?? 'gray'}>{VERIFICATION_LABEL[status] ?? status}</Chip>
}

export default async function OpsTenantsIndexPage() {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/team/ops/login?next=/team/ops/tenants')
    throw err
  }

  let rows: TenantIndexRow[] = []
  let error: string | null = null
  try {
    rows = await fetchAssignedTenants(operator)
  } catch (err) {
    error = err instanceof Error ? err.message : 'unknown error'
    console.error('OpsTenantsIndexPage: fetchAssignedTenants failed', err)
  }

  const scopeLabel = operator.assignedTenants === null ? 'all businesses' : 'your businesses'

  return (
    <main className="ops-tenants-index min-h-screen space-y-6 bg-background p-6" data-area="team-ops-tenants-index">
      <header className="rounded-lg border border-border bg-card p-6 shadow-sm">
        <h1 className="text-2xl font-semibold text-foreground">Tenants ({scopeLabel})</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Businesses you are assigned to. Open one for its full profile, plan and agents.
        </p>
      </header>

      {error ? (
        <section className="rounded-lg border border-border bg-card p-6 shadow-sm">
          <p data-section-error className="text-sm text-destructive">
            couldn&apos;t load tenants: {error}
          </p>
        </section>
      ) : rows.length === 0 ? (
        <section className="rounded-lg border border-border bg-card p-6 shadow-sm">
          <p data-ops-empty className="text-sm text-muted-foreground">
            No tenants assigned to you yet.
          </p>
        </section>
      ) : (
        <section className="overflow-hidden rounded-lg border border-border bg-card shadow-sm">
          <table data-ops-tenants-index className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-border text-[11px] uppercase tracking-wide text-muted-foreground">
                <th className="px-4 py-3 font-medium">Business</th>
                <th className="px-4 py-3 font-medium">Verification</th>
                <th className="px-4 py-3 font-medium">Phase</th>
                <th className="px-4 py-3 font-medium">Plan</th>
                <th className="px-4 py-3 font-medium">Added</th>
                <th className="px-4 py-3 font-medium" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((r) => (
                <tr key={r.tenant_id} data-tenant-id={r.tenant_id} className="hover:bg-muted/40">
                  <td className="px-4 py-3">
                    <div className="font-medium text-foreground">{r.business_name ?? '—'}</div>
                    <code className="font-mono text-[11px] text-muted-foreground">{r.tenant_id}</code>
                  </td>
                  <td className="px-4 py-3">
                    <VerificationChip status={r.verification_status} />
                  </td>
                  <td className="px-4 py-3">{r.phase ? <Chip tone="blue">{r.phase}</Chip> : '—'}</td>
                  <td className="px-4 py-3">{r.plan_tier ? <Chip tone="gray">{r.plan_tier}</Chip> : '—'}</td>
                  <td className="px-4 py-3 text-foreground">{fmtDate(r.created_at)}</td>
                  <td className="px-4 py-3 text-right">
                    <Link
                      href={`/team/ops/tenants/${r.tenant_id}`}
                      className="rounded-md border border-input px-3 py-1.5 text-sm text-foreground hover:bg-muted"
                    >
                      Open →
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </main>
  )
}
