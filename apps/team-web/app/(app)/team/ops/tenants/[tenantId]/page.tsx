/** Ops Console — per-tenant dashboard (VT-123 view 2 of 3). */

import { notFound, redirect } from 'next/navigation'

import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import {
  fetchPrivacyAudit,
  fetchRecentCampaigns,
  fetchTenantProfile,
  fetchTenantTimeline,
} from '@/lib/ops/data-access'

export const dynamic = 'force-dynamic'

interface PageProps {
  params: Promise<{ tenantId: string }>
}

export default async function TenantDashboardPage({ params }: PageProps) {
  try {
    await requireFazal()
  } catch (err) {
    if (err instanceof UnauthorizedError) redirect('/login')
    throw err
  }
  const { tenantId } = await params

  const [profile, timeline, campaigns, audit] = await Promise.all([
    fetchTenantProfile(tenantId),
    fetchTenantTimeline(tenantId, 30),
    fetchRecentCampaigns(tenantId, 10),
    fetchPrivacyAudit(tenantId, 20),
  ])

  if (!profile) notFound()

  return (
    <main className="ops-tenant" data-area="team-ops-tenant" data-tenant-id={tenantId}>
      <header>
        <h1>Tenant — {profile.business_name ?? profile.tenant_id}</h1>
        <p>
          phase: {profile.phase} | plan: {profile.plan_tier} | tenant_id:{' '}
          <code>{profile.tenant_id}</code>
        </p>
      </header>

      <section data-section="timeline">
        <h2>30-day pipeline runs ({timeline.length})</h2>
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>Status</th>
              <th>Trigger</th>
              <th>Started</th>
              <th>Ended</th>
              <th>Cost (paise)</th>
              <th>Steps</th>
            </tr>
          </thead>
          <tbody>
            {timeline.map((r) => (
              <tr key={r.run_id}>
                <td>
                  <a href={`/team/ops/runs/${r.run_id}`}>{r.run_id}</a>
                </td>
                <td>{r.status}</td>
                <td>{r.trigger_kind ?? '—'}</td>
                <td>{new Date(r.started_at).toLocaleString()}</td>
                <td>{r.ended_at ? new Date(r.ended_at).toLocaleString() : '—'}</td>
                <td>{r.total_cost_paise ?? '—'}</td>
                <td>{r.step_count ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section data-section="campaigns">
        <h2>Recent campaigns</h2>
        <table>
          <thead>
            <tr>
              <th>Campaign</th>
              <th>Status</th>
              <th>Generated</th>
            </tr>
          </thead>
          <tbody>
            {campaigns.map((c) => (
              <tr key={c.campaign_id}>
                <td>{c.campaign_id}</td>
                <td>{c.status}</td>
                <td>{new Date(c.generated_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section data-section="privacy-audit">
        <h2>Privacy audit log</h2>
        <table>
          <thead>
            <tr>
              <th>Event</th>
              <th>Actor</th>
              <th>When</th>
              <th>Payload</th>
            </tr>
          </thead>
          <tbody>
            {audit.map((a) => (
              <tr key={a.id}>
                <td>{a.event_type}</td>
                <td>{a.actor ?? '—'}</td>
                <td>{new Date(a.created_at).toLocaleString()}</td>
                <td>
                  <code>{JSON.stringify(a.payload)}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  )
}
