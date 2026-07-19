/**
 * VT-405 — the VTR tenant discovery panel.
 *
 * Renders one tenant's signup fields + auto-discovered profile draft + confirmation status from the
 * vtr_tenant_profile view (non-PII; WhatsApp last-4 only; confirmed profile keys-only). Server
 * component; Part B adds the per-row Confirm action via a small client island (ConfirmFieldButton)
 * — a VTR may confirm ANY discovered field incl. identity (CL-441), promoting it to VTR-asserted
 * (badged distinctly from owner-confirmed; owner-confirm supersedes).
 */

import type { VtrTenantProfile } from '@/lib/orchestrator-client'
import { ConfirmFieldButton } from '@/components/ops/confirm-field-button'

function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString()
}

function fmtValue(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (Array.isArray(v)) return v.map((x) => String(x)).join(' · ')
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

const ONBOARDING_STAGES = ['signed up', 'discovered', 'awaiting reply', 'confirm', 'first plan'] as const

function currentStage(p: VtrTenantProfile): (typeof ONBOARDING_STAGES)[number] {
  const hasDraft = p.draft_attributes && Object.keys(p.draft_attributes).length > 0
  const hasPlan = false // first-plan signal not in this view; reserved
  if (hasPlan) return 'first plan'
  if ((p.confirmed_fields?.length ?? 0) > 0) return 'confirm'
  // active journey + empty queue + a draft = the owner hasn't replied yet (VT-diagnosis)
  if (p.onboarding_status === 'active' && p.onboarding_queue_len === 0 && hasDraft) return 'awaiting reply'
  if (hasDraft) return 'discovered'
  return 'signed up'
}

function Chip({ children, tone }: { children: React.ReactNode; tone: 'gray' | 'blue' | 'green' | 'amber' }) {
  const tones: Record<string, string> = {
    gray: 'bg-gray-100 text-gray-700 border-gray-200',
    blue: 'bg-blue-50 text-blue-700 border-blue-200',
    green: 'bg-green-50 text-green-700 border-green-200',
    amber: 'bg-amber-50 text-amber-800 border-amber-200',
  }
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${tones[tone]}`}>
      {children}
    </span>
  )
}

function StatTile({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className="mt-0.5 text-sm font-medium text-gray-900">{value}</div>
    </div>
  )
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-gray-200 bg-white p-6 shadow-sm">
      <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-gray-500">{title}</h2>
      {children}
    </section>
  )
}

export function TenantDiscoveryPanel({ profile: p }: { profile: VtrTenantProfile }) {
  const draftKeys = p.draft_attributes ? Object.keys(p.draft_attributes).sort() : []
  const confirmed = new Set(p.confirmed_fields ?? [])
  const stage = currentStage(p)

  return (
    <div className="space-y-6" data-section="discovery-panel">
      {/* 1. Header */}
      <section className="rounded-lg border border-gray-200 bg-white p-6 shadow-sm">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-gray-900">{p.business_name ?? p.tenant_id}</h1>
          {p.phase && <Chip tone="blue">{p.phase}</Chip>}
          {p.plan_tier && <Chip tone="gray">{p.plan_tier}</Chip>}
        </div>
        <p className="mt-1 text-xs text-gray-500">
          tenant_id <code className="font-mono text-gray-700">{p.tenant_id}</code>
        </p>
        <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatTile label="Signed up" value={fmtDate(p.signed_up_at)} />
          <StatTile label="Trial started" value={fmtDate(p.trial_started_at)} />
          <StatTile label="Onboarding" value={stage} />
          <StatTile label="Language" value={p.language_preference ?? p.preferred_language ?? '—'} />
        </div>
      </section>

      {/* 5. Onboarding strip + controls */}
      <Card title="Onboarding">
        <ol className="flex flex-wrap items-center gap-2 text-xs">
          {ONBOARDING_STAGES.map((s, i) => {
            const reached = ONBOARDING_STAGES.indexOf(stage) >= i
            const isCurrent = s === stage
            return (
              <li key={s} className="flex items-center gap-2">
                <span
                  className={`rounded-full px-2 py-0.5 ${
                    isCurrent
                      ? 'bg-amber-100 font-semibold text-amber-900'
                      : reached
                        ? 'bg-green-50 text-green-700'
                        : 'bg-gray-100 text-gray-400'
                  }`}
                >
                  {s}
                </span>
                {i < ONBOARDING_STAGES.length - 1 && <span className="text-gray-300">→</span>}
              </li>
            )
          })}
        </ol>
        <div className="mt-4 flex flex-wrap gap-2">
          <a
            href={`/team/ops/tenants/${p.tenant_id}/plan`}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50"
          >
            Plan
          </a>
          <a
            href={`/team/ops/tenants/${p.tenant_id}/agents`}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50"
          >
            Agents
          </a>
          {/* Placeholders — clearly disabled until wired (Cowork spec). */}
          {['Message founder', 'Pause', 'Run control', 'Escalate'].map((b) => (
            <button
              key={b}
              disabled
              title="not wired yet"
              className="cursor-not-allowed rounded-md border border-dashed border-gray-300 px-3 py-1.5 text-sm text-gray-400"
            >
              {b}
            </button>
          ))}
        </div>
      </Card>

      {/* 2. Auto-discovered profile (hero) */}
      <Card title="Auto-discovered profile">
        {draftKeys.length === 0 ? (
          <p className="text-sm text-gray-500">
            Not yet enriched — auto-discovery (GBP / website) hasn&apos;t produced a draft for this tenant.
          </p>
        ) : (
          <div className="divide-y divide-gray-100">
            {draftKeys.map((k) => {
              const source = p.draft_provenance?.[k]?.source ?? null
              const isConfirmed = confirmed.has(k)
              const prov = p.field_provenance?.[k]
              return (
                <div key={k} className="flex items-start gap-3 py-2.5">
                  <div className="w-36 shrink-0 text-xs font-medium uppercase tracking-wide text-gray-500">
                    {k.replace(/_/g, ' ')}
                  </div>
                  <div className="min-w-0 flex-1 text-sm text-gray-900">
                    {fmtValue(p.draft_attributes?.[k])}
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    {source && <Chip tone="gray">{source}</Chip>}
                    {isConfirmed ? (
                      prov?.source === 'vtr' ? (
                        <Chip tone="blue">VTR-asserted</Chip>
                      ) : (
                        <Chip tone="green">owner-confirmed</Chip>
                      )
                    ) : (
                      <>
                        <Chip tone="amber">discovered</Chip>
                        <ConfirmFieldButton tenantId={p.tenant_id} field={k} />
                      </>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </Card>

      {/* 3. Verified profile (keys-only) */}
      <Card title="Verified profile">
        {confirmed.size === 0 ? (
          <p className="text-sm text-gray-500">No fields confirmed yet.</p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {[...confirmed].sort().map((k) => (
              <Chip key={k} tone="green">
                {k.replace(/_/g, ' ')}
              </Chip>
            ))}
          </div>
        )}
        <p className="mt-3 text-[11px] text-gray-400">
          Confirmation is key-presence only — confirmed values are not exposed on the VTR surface (PII boundary).
        </p>
      </Card>

      {/* 4. Founder & signup */}
      <Card title="Founder &amp; signup">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
          <div>
            <dt className="text-xs text-gray-500">Owner</dt>
            <dd className="text-gray-900">{p.owner_name ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-xs text-gray-500">WhatsApp</dt>
            <dd className="font-mono text-gray-900">{p.whatsapp_last4 ? `••• ${p.whatsapp_last4}` : '—'}</dd>
          </div>
          <div>
            <dt className="text-xs text-gray-500">Business type</dt>
            <dd className="text-gray-900">{p.business_type ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-xs text-gray-500">Locality</dt>
            <dd className="text-gray-900">
              {p.locality ?? '—'}
              {p.city_tier ? ` (${p.city_tier})` : ''}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-gray-500">Language</dt>
            <dd className="text-gray-900">{p.language_preference ?? p.preferred_language ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-xs text-gray-500">Signed up</dt>
            <dd className="text-gray-900">{fmtDate(p.signed_up_at)}</dd>
          </div>
        </dl>
      </Card>
    </div>
  )
}
