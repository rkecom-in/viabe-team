/**
 * VT-704 — Tenant ACTIVITY FLOW: the 30-day time-based conversation + decision + execution
 * flow. STYLED to match the e2e-sim flow-diagram artifact (Fazal 2026-07-27: "expected to be
 * designed/styled like the conversation flow in this url") — warm green-tinted paper, a
 * center timeline rail, kind-chips riding the rail, WhatsApp-style owner/Manager bubbles,
 * severity-striped system cards, day pills, and a checks-style summary strip.
 *
 * Light-mode-only by ops-console convention (the tm-activity-feed precedent); the palette is
 * the artifact's: paper #F6F4EE · ink #1C2B25 · rail #C9D4CC · owner bubble #D8EFCB ·
 * accent #0E7A5F · warn #B4560F.
 *
 * Guarded per-page (requireOpsOperator) + tenant-scoped (canAccessTenant, deny render).
 */

import { redirect } from 'next/navigation'

import { UnauthorizedError } from '@/lib/auth/require-fazal'
import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { canAccessTenant } from '@/lib/ops/assignments'
import {
  fetchTenantFlow,
  groupByDay,
  type FlowEvent,
} from '@/lib/ops/activity-flow'

export const dynamic = 'force-dynamic'

interface PageProps {
  params: Promise<{ tenantId: string }>
}

const KIND_CHIP: Record<FlowEvent['kind'], string> = {
  message: 'MESSAGE',
  decision: 'MANAGER DECISION',
  task: 'SUB-AGENT',
  step_error: 'EXECUTION ERROR',
  approval: 'APPROVAL',
  comms: 'COMMS',
  incident: 'INCIDENT',
  alert: 'ALERT',
}

// The artifact's chip logic: LLM/decision moments carry the accent chip; deterministic
// delivery/infra events stay quiet grey.
const HOT_KINDS: ReadonlySet<FlowEvent['kind']> = new Set(['decision', 'task'])

const SEV_STRIPE: Record<FlowEvent['severity'], string> = {
  info: 'border-l-[#C9D4CC]',
  warn: 'border-l-[#B4560F]',
  error: 'border-l-[#c03434]',
}

function Chip({ e }: { e: FlowEvent }) {
  const hot = HOT_KINDS.has(e.kind) && e.severity === 'info'
  return (
    <span
      className={`absolute left-1/2 -translate-x-1/2 -top-2.5 z-10 rounded-full px-3 py-0.5 font-mono text-[10px] tracking-[0.06em] whitespace-nowrap ${
        hot ? 'bg-[#0E7A5F] text-white' : 'bg-[#EAEFE9] text-[#43554C]'
      }`}
    >
      {KIND_CHIP[e.kind]}
    </span>
  )
}

function SystemCard({ e }: { e: FlowEvent }) {
  const flagged = e.severity !== 'info'
  return (
    <div className="relative pt-3">
      <Chip e={e} />
      <div
        className={`relative z-[1] mx-auto w-full max-w-2xl rounded-md border border-[#DDE4DD] border-l-4 ${SEV_STRIPE[e.severity]} px-4 py-2.5 ${
          flagged ? 'bg-[#FBEFE3]' : 'bg-white'
        }`}
      >
        <div className="flex items-baseline justify-between gap-3">
          <p className="text-sm font-medium text-[#1C2B25]">{e.title}</p>
          <span className="font-mono text-[11px] text-[#93A399]">
            {String(e.ts).slice(11, 19)}Z
          </span>
        </div>
        {e.body ? (
          <p className="mt-1 text-xs whitespace-pre-wrap text-[#5C6B63]">{e.body}</p>
        ) : null}
        {Object.entries(e.meta).filter(([, v]) => v).length > 0 ? (
          <p className="mt-1 font-mono text-[10px] text-[#93A399]">
            {Object.entries(e.meta)
              .filter(([, v]) => v)
              .map(([k, v]) => `${k}=${v}`)
              .join('  ')}
          </p>
        ) : null}
      </div>
    </div>
  )
}

function MessageBubble({ e }: { e: FlowEvent }) {
  const owner = e.lane === 'owner'
  return (
    <div className={`relative z-[1] flex ${owner ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[46%] rounded-xl px-3.5 py-2 text-sm max-md:max-w-[72%] ${
          owner
            ? 'rounded-br-[4px] bg-[#D8EFCB] text-[#1E3320]'
            : 'rounded-bl-[4px] border border-[#DDE4DD] bg-white text-[#1C2B25]'
        }`}
      >
        <div className="flex items-baseline gap-2">
          <span className="text-[11px] font-medium text-[#5C6B63]">{e.title}</span>
          <span className="font-mono text-[10px] text-[#93A399]">
            {String(e.ts).slice(11, 16)}Z
          </span>
        </div>
        <p className="mt-0.5 whitespace-pre-wrap">{e.body}</p>
      </div>
    </div>
  )
}

function SummaryStrip({ events }: { events: FlowEvent[] }) {
  const n = (k: FlowEvent['kind']) => events.filter((e) => e.kind === k).length
  const cells: Array<[string, number]> = [
    ['owner ↔ manager messages', n('message')],
    ['manager decisions', n('decision')],
    ['sub-agent tasks', n('task')],
    ['approvals', n('approval')],
    ['comms deliveries', n('comms')],
    ['errors & incidents', n('step_error') + n('incident') + n('alert')],
  ]
  return (
    <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-2.5">
      {cells.map(([label, count]) => (
        <div
          key={label}
          className="flex items-baseline gap-2 rounded-lg border border-[#DDE4DD] bg-white px-3.5 py-2 text-sm"
        >
          <b className={`font-semibold ${label.startsWith('errors') && count > 0 ? 'text-[#B4560F]' : 'text-[#0E7A5F]'}`}>
            {count}
          </b>
          <span className="text-[#5C6B63]">{label}</span>
        </div>
      ))}
    </div>
  )
}

export default async function TenantActivityFlowPage({ params }: PageProps) {
  const { tenantId } = await params
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      redirect(`/team/ops/login?next=/team/ops/tenants/${tenantId}/activity-flow`)
    }
    throw err
  }

  if (!canAccessTenant(operator.assignedTenants, tenantId)) {
    return (
      <main className="min-h-screen bg-[#F6F4EE] p-6" data-area="team-ops-activity-flow">
        <p className="text-sm text-[#B4560F]">Not assigned to this tenant.</p>
      </main>
    )
  }

  const { events, counts } = await fetchTenantFlow(operator, tenantId)
  const days = groupByDay(events)
  const capped = Object.entries(counts).filter(([, c]) => c.fetched >= c.cap)

  return (
    <main
      className="min-h-screen bg-[#F6F4EE] px-4 pb-16 text-[#1C2B25]"
      data-area="team-ops-activity-flow"
      data-tenant-id={tenantId}
    >
      <div className="mx-auto max-w-3xl">
        <header className="space-y-3 pt-10 pb-2">
          <h1 className="text-[1.6rem] leading-tight font-semibold [text-wrap:balance]">
            Activity flow — last 30 days
          </h1>
          <p className="text-sm text-[#5C6B63]">
            tenant_id <code className="font-mono text-xs text-[#43554C]">{tenantId}</code>
            {' '}· {events.length} events
          </p>
          <p className="text-xs text-[#5C6B63]">
            <a className="underline" href={`/team/ops/tenants/${tenantId}`}>
              ← tenant dashboard
            </a>{' '}
            ·{' '}
            <a className="underline" href={`/team/ops/tenants/${tenantId}/plan`}>
              plan →
            </a>
          </p>
          <SummaryStrip events={events} />
          <div className="flex flex-wrap gap-4 pt-1 text-xs text-[#5C6B63]">
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-2.5 w-2.5 rounded-full bg-[#0E7A5F]" /> manager
              decision / sub-agent
            </span>
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-2.5 w-2.5 rounded-full border border-[#C9D4CC] bg-[#EAEFE9]" />{' '}
              delivery / infra event
            </span>
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-2.5 w-2.5 rounded-full bg-[#B4560F]" /> warning /
              error stripe
            </span>
          </div>
          {capped.length > 0 ? (
            <p className="text-xs text-[#B4560F]" data-flow-truncation>
              Truncated sources (showing newest rows only):{' '}
              {capped.map(([name, c]) => `${name} (${c.cap})`).join(', ')}
            </p>
          ) : null}
        </header>

        {events.length === 0 ? (
          <p className="text-sm text-[#5C6B63]">No activity recorded in the last 30 days.</p>
        ) : (
          days.map(({ day, events: dayEvents }) => (
            <section
              key={day}
              className="relative mt-2 space-y-4 pb-4 before:absolute before:top-0 before:bottom-0 before:left-1/2 before:w-[2px] before:-translate-x-1/2 before:bg-[#C9D4CC] before:content-[''] max-md:before:left-6"
              data-flow-day={day}
            >
              <h2 className="sticky top-2 z-20 mx-auto w-fit rounded-full bg-[#1C2B25] px-4 py-1 text-xs font-medium text-white">
                {day}
              </h2>
              {dayEvents.map((e, i) =>
                e.kind === 'message' ? (
                  <MessageBubble key={`${day}-${i}`} e={e} />
                ) : (
                  <SystemCard key={`${day}-${i}`} e={e} />
                ),
              )}
            </section>
          ))
        )}
      </div>
    </main>
  )
}
