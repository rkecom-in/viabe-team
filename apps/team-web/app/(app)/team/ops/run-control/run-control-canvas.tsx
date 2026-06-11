'use client'

/**
 * VT-375 (Phase B) — Run-Control canvas client components (READ-ONLY).
 *
 * Renders the tenant-tile → program-tile → step-timeline hierarchy from data fetched
 * server-side (the page owns the orchestrator secret/JWT; this component receives plain
 * de-identified projections). The ONLY interactivity is local expand/collapse — zero
 * mutating calls, zero buttons that change server state.
 *
 * Binding honesty copy (verbatim-or-equivalent, all visible):
 *   - observed-tier steps → badge "Observed — not controllable"
 *   - rerun lineage → "Re-dispatched as a NEW run (no time-travel) — prior steps re-execute
 *     only if the entry point requires them."
 *   - envelope cells → "Keys only — values are de-identified by construction."
 *   - degraded → banner "Pause state unverifiable right now — control reads are degraded."
 *   - holds footer → "Concurrently-held runs release in no guaranteed order."
 *
 * data-testid hooks: rc-tenant-tile, rc-program-tile, rc-timeline-row, rc-degraded-banner.
 */

import { useState } from 'react'

import type {
  VtrHold,
  VtrProgramRun,
  VtrProgramsResult,
  VtrRunTimelineResult,
  VtrTimelineStep,
  VtrUpcomingItem,
} from '@/lib/orchestrator-client'

export interface TenantCanvasData {
  tenantId: string
  tenantName: string | null
  programs: VtrProgramsResult
  timelines: Record<string, VtrRunTimelineResult>
}

const ENVELOPE_KEYS_COPY = 'Keys only — values are de-identified by construction.'
const RERUN_LINEAGE_COPY =
  'Re-dispatched as a NEW run (no time-travel) — prior steps re-execute only if the entry point requires them.'
const OBSERVED_BADGE_COPY = 'Observed — not controllable'
const DEGRADED_COPY = 'Pause state unverifiable right now — control reads are degraded.'
const HOLDS_FOOTER_COPY = 'Concurrently-held runs release in no guaranteed order.'

function fmtTs(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(1)} s`
}

/** Render envelope as either a keys-only list (array) or a de-identified object summary. */
function envelopeKeys(envelope: unknown): string {
  if (envelope == null) return '—'
  if (Array.isArray(envelope)) return envelope.length ? envelope.join(', ') : '(no keys)'
  if (typeof envelope === 'object') return Object.keys(envelope as object).join(', ') || '(no keys)'
  return String(envelope)
}

function StatusPill({ status }: { status: string | null | undefined }) {
  const s = status ?? 'unknown'
  const tone =
    s === 'running'
      ? 'bg-blue-50 text-blue-700 border-blue-200'
      : s === 'completed' || s === 'succeeded'
        ? 'bg-green-50 text-green-700 border-green-200'
        : s === 'failed' || s === 'aborted_hard_limit' || s === 'escalated'
          ? 'bg-red-50 text-red-700 border-red-200'
          : 'bg-gray-50 text-gray-600 border-gray-200'
  return (
    <span className={`inline-block rounded border px-1.5 py-0.5 text-xs font-medium ${tone}`}>
      {s}
    </span>
  )
}

/** One run's step-timeline table — keys-only envelopes, observed-tier badge, lineage note. */
function StepTimeline({ timeline }: { timeline: VtrRunTimelineResult | undefined }) {
  if (!timeline) {
    return <p className="text-xs text-gray-400 px-3 py-2">Timeline not loaded.</p>
  }
  if (!timeline.ok) {
    return (
      <p data-section-error className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2">
        couldn&apos;t load timeline ({timeline.reason}).
      </p>
    )
  }
  const steps = timeline.steps.filter((s) => s.step_seq != null)
  const reruns = timeline.steps.find((s) => s.rerun_of_run_id)
  return (
    <div className="space-y-2">
      {reruns?.rerun_of_run_id && (
        <p data-rc-rerun-note className="text-xs text-gray-600 bg-gray-50 border border-gray-200 rounded p-2">
          {RERUN_LINEAGE_COPY}{' '}
          <span className="font-mono text-gray-400">
            (from {reruns.rerun_of_run_id}
            {reruns.rerun_from_step ? ` @ ${reruns.rerun_from_step}` : ''})
          </span>
        </p>
      )}
      {steps.length === 0 ? (
        <p className="text-xs text-gray-400 px-1 py-1">No steps recorded for this run.</p>
      ) : (
        <table className="min-w-full divide-y divide-gray-200 text-xs">
          <thead className="bg-gray-50">
            <tr>
              {['Seq', 'Step', 'Status', 'Duration', 'Override', 'Paused', 'Envelope keys'].map((h) => (
                <th key={h} className="px-2 py-1 text-left font-medium uppercase text-gray-500">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {steps.map((s) => (
              <TimelineRow key={s.step_id ?? `${s.step_seq}`} step={s} />
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function TimelineRow({ step }: { step: VtrTimelineStep }) {
  // Control axis is orchestrator-authoritative: badge when the step is NOT controllable.
  // Absent/unknown tier ⇒ treat as observed (fail-safe — never imply controllable on silence).
  const observed = step.tier !== 'controllable'
  return (
    <tr data-testid="rc-timeline-row" className="hover:bg-gray-50">
      <td className="px-2 py-1 text-gray-500 font-mono">{step.step_seq}</td>
      <td className="px-2 py-1">
        <span className="font-mono text-gray-700">
          {step.step_kind ?? '—'}
          {step.step_name ? `:${step.step_name}` : ''}
        </span>
        {observed && (
          <span
            data-rc-observed-badge
            className="ml-1 inline-block rounded border border-gray-300 bg-gray-50 px-1 py-0.5 text-[10px] font-medium text-gray-500"
            title="Observed-tier step — timeline display only; not controllable in this panel."
          >
            {OBSERVED_BADGE_COPY}
          </span>
        )}
      </td>
      <td className="px-2 py-1">
        <StatusPill status={step.step_status} />
      </td>
      <td className="px-2 py-1 text-gray-600">{fmtDuration(step.duration_ms)}</td>
      <td className="px-2 py-1 font-mono text-gray-400">{step.override_id ?? '—'}</td>
      <td className="px-2 py-1 text-gray-600">{step.paused_ms != null ? fmtDuration(step.paused_ms) : '—'}</td>
      <td className="px-2 py-1 text-gray-600">
        <span className="font-mono">{envelopeKeys(step.input_envelope)}</span>
        <span className="ml-1 block text-[10px] text-gray-400" title={ENVELOPE_KEYS_COPY}>
          {ENVELOPE_KEYS_COPY}
        </span>
      </td>
    </tr>
  )
}

/** One run row inside a program group, with an expandable step timeline. */
function ProgramRunTile({
  run,
  group,
  timeline,
}: {
  run: VtrProgramRun
  group: 'past' | 'running'
  timeline: VtrRunTimelineResult | undefined
}) {
  const [open, setOpen] = useState(group === 'running')
  return (
    <div data-testid="rc-program-tile" className="rounded border border-gray-200 bg-white">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-2 text-left hover:bg-gray-50"
        aria-expanded={open}
      >
        <span className="flex items-center gap-2">
          <span className="font-mono text-xs text-gray-700">{run.run_id}</span>
          {run.run_type && <span className="text-xs text-gray-400">{run.run_type}</span>}
          <StatusPill status={run.status} />
          {run.active_hold && (
            <span className="rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
              held
            </span>
          )}
          {run.rerun_of_run_id && (
            <span className="rounded border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 text-[10px] font-medium text-indigo-600">
              re-dispatched
            </span>
          )}
        </span>
        <span className="text-xs text-gray-400">
          {fmtTs(run.started_at)} · {run.step_count ?? '—'} steps · {open ? 'hide' : 'show'}
        </span>
      </button>
      {open && (
        <div className="border-t border-gray-100 px-3 py-2">
          <StepTimeline timeline={timeline} />
        </div>
      )}
    </div>
  )
}

function UpcomingTile({ item }: { item: VtrUpcomingItem }) {
  return (
    <div data-testid="rc-program-tile" className="rounded border border-gray-200 bg-white px-3 py-2">
      <div className="flex items-center justify-between">
        <span className="text-sm text-gray-700">{item.label}</span>
        <span className="rounded border border-gray-200 bg-gray-50 px-1.5 py-0.5 text-[10px] font-medium text-gray-500">
          {item.kind}
        </span>
      </div>
      <div className="mt-0.5 flex items-center justify-between text-[11px] text-gray-400">
        <span>{fmtTs(item.due_at)}</span>
        <span className="font-mono">{item.source}</span>
      </div>
    </div>
  )
}

function HoldsList({ holds }: { holds: VtrHold[] }) {
  if (holds.length === 0) return null
  return (
    <div className="rounded border border-amber-200 bg-amber-50 p-2">
      <p className="text-xs font-medium text-amber-800">Active holds</p>
      <ul className="mt-1 space-y-0.5">
        {holds.map((h, i) => (
          <li key={`${h.workflow_kind}-${i}`} className="text-xs text-amber-700">
            <span className="font-mono">{h.workflow_kind}</span> · held since {fmtTs(h.set_at)}
          </li>
        ))}
      </ul>
      <p data-rc-holds-footer className="mt-1 text-[11px] italic text-amber-600">
        {HOLDS_FOOTER_COPY}
      </p>
    </div>
  )
}

function ProgramGroup({
  title,
  children,
}: {
  title: string
  children: React.ReactNode
}) {
  return (
    <div className="space-y-1.5">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">{title}</h4>
      {children}
    </div>
  )
}

function TenantTile({ data }: { data: TenantCanvasData }) {
  const { programs } = data
  const [open, setOpen] = useState(false)
  return (
    <section
      data-testid="rc-tenant-tile"
      className="rounded-lg border border-gray-200 bg-white shadow-sm"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-gray-50"
        aria-expanded={open}
      >
        <span className="flex items-center gap-2">
          <span className="text-sm font-medium text-gray-900">
            {data.tenantName ?? data.tenantId}
          </span>
          <span className="font-mono text-[11px] text-gray-400">{data.tenantId}</span>
        </span>
        <span className="flex items-center gap-2 text-xs text-gray-500">
          <span>{programs.running.length} running</span>
          <span>·</span>
          <span>{programs.past.length} past</span>
          <span>·</span>
          <span>{programs.upcoming7d.length} upcoming</span>
          {programs.holds.length > 0 && (
            <span className="rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 font-medium text-amber-700">
              {programs.holds.length} held
            </span>
          )}
          <span className="text-gray-400">{open ? 'collapse' : 'expand'}</span>
        </span>
      </button>

      {open && (
        <div className="space-y-3 border-t border-gray-100 p-4">
          {programs.degraded && (
            <p
              data-testid="rc-degraded-banner"
              className="rounded border border-amber-300 bg-amber-50 p-2 text-xs font-medium text-amber-800"
            >
              {DEGRADED_COPY}
            </p>
          )}

          <HoldsList holds={programs.holds} />

          <ProgramGroup title="Running">
            {programs.running.length === 0 ? (
              <p className="text-xs text-gray-400">No running programs.</p>
            ) : (
              programs.running.map((run) => (
                <ProgramRunTile
                  key={run.run_id}
                  run={run}
                  group="running"
                  timeline={data.timelines[run.run_id]}
                />
              ))
            )}
          </ProgramGroup>

          <ProgramGroup title="Past">
            {programs.past.length === 0 ? (
              <p className="text-xs text-gray-400">No past programs.</p>
            ) : (
              programs.past.map((run) => (
                <ProgramRunTile
                  key={run.run_id}
                  run={run}
                  group="past"
                  timeline={data.timelines[run.run_id]}
                />
              ))
            )}
          </ProgramGroup>

          <ProgramGroup title="Upcoming 7d">
            {programs.upcoming7d.length === 0 ? (
              <p className="text-xs text-gray-400">Nothing forecast in the next 7 days.</p>
            ) : (
              programs.upcoming7d.map((item, i) => <UpcomingTile key={`${item.kind}-${i}`} item={item} />)
            )}
          </ProgramGroup>
        </div>
      )}
    </section>
  )
}

export function RunControlCanvas({ tenants }: { tenants: TenantCanvasData[] }) {
  return (
    <div data-rc-canvas className="space-y-3">
      {tenants.map((t) => (
        <TenantTile key={t.tenantId} data={t} />
      ))}
    </div>
  )
}
