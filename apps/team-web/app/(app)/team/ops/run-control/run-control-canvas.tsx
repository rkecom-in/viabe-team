'use client'

/**
 * Run-Control canvas client components (VT-375 Phase B read surface + VT-376 Phase C controls).
 *
 * Renders the tenant-tile → program-tile → step-timeline hierarchy from data fetched
 * server-side (the page owns the orchestrator secret/JWT; this component receives plain
 * de-identified projections). VT-376 (Phase C): the tiles/timeline render the control
 * components (PauseReleaseControls, OverrideControl, RerunControl from ./run-control-controls)
 * which invoke the server actions in ./actions.ts (pause/release/override/rerun) — each gated
 * server-side. Read projections stay de-identified; mutations go through the gated actions only.
 *
 * Binding honesty copy (verbatim-or-equivalent, all visible):
 *   - observed-tier steps → badge "Observed — not controllable"
 *   - rerun lineage → "Re-dispatched as a NEW run (no time-travel) — prior steps re-execute
 *     only if the entry point requires them."
 *   - rerun lineage (VT-375 C1, Option A) → "A re-run that overlapped an owner decision is
 *     escalated, not silently kept." Run rows carry status 'escalated' (the overlap close);
 *     the overlap METADATA does not ride the read projections, so the disclosure is the
 *     static line + an "escalated re-run" marker on escalated lineage rows.
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

import {
  OverrideControl,
  PauseReleaseControls,
  RerunControl,
} from './run-control-controls'

// The workflow kinds an operator can pause/release from the tenant tile (the migration-131
// workflow_controls CHECK list — registry.WORKFLOW_KINDS). Pinned here so the pause tiles render
// even when no hold is active for a kind (you can pause a kind that isn't currently held).
const PAUSEABLE_KINDS = [
  'webhook_inbound',
  'agent_dispatch',
  'auto_discovery',
  'plan_generate',
  'plan_deliver',
  'trial_sweep',
  'ingestion',
  'campaign_send',
] as const

/** run_type → workflow_kind, mirroring the orchestrator's RUN_TYPE_TO_KIND for override scoping. */
const RUN_TYPE_TO_KIND: Record<string, string> = {
  webhook_inbound: 'webhook_inbound',
  agent_dispatch: 'agent_dispatch',
  auto_discovery: 'auto_discovery',
  plan_generate: 'plan_generate',
  plan_deliver: 'plan_deliver',
  trial_sweep: 'trial_sweep',
  ingestion: 'ingestion',
  campaign_send: 'campaign_send',
}

export interface TenantCanvasData {
  tenantId: string
  tenantName: string | null
  programs: VtrProgramsResult
  timelines: Record<string, VtrRunTimelineResult>
}

const ENVELOPE_KEYS_COPY = 'Keys only — values are de-identified by construction.'
const RERUN_LINEAGE_COPY =
  'Re-dispatched as a NEW run (no time-travel) — prior steps re-execute only if the entry point requires them.'
const RERUN_OVERLAP_COPY =
  'A re-run that overlapped an owner decision is escalated, not silently kept.'
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
      ? 'bg-primary/10 text-primary border-primary/30'
      : s === 'completed' || s === 'succeeded'
        ? 'bg-secondary/10 text-secondary border-secondary/30'
        : s === 'failed' || s === 'aborted_hard_limit' || s === 'escalated'
          ? 'bg-destructive/10 text-destructive border-destructive/30'
          : 'bg-muted text-muted-foreground border-border'
  return (
    <span className={`inline-block rounded border px-1.5 py-0.5 text-xs font-medium ${tone}`}>
      {s}
    </span>
  )
}

/** One run's step-timeline table — keys-only envelopes, observed-tier badge, lineage note. */
function StepTimeline({
  timeline,
  run,
}: {
  timeline: VtrRunTimelineResult | undefined
  run: VtrProgramRun
}) {
  if (!timeline) {
    return <p className="text-xs text-muted-foreground px-3 py-2">Timeline not loaded.</p>
  }
  if (!timeline.ok) {
    return (
      <p data-section-error className="text-xs text-gold-foreground bg-gold/15 border border-gold/40 rounded p-2">
        couldn&apos;t load timeline ({timeline.reason}).
      </p>
    )
  }
  const steps = timeline.steps.filter((s) => s.step_seq != null)
  const reruns = timeline.steps.find((s) => s.rerun_of_run_id)
  const workflowKind = RUN_TYPE_TO_KIND[run.run_type ?? ''] ?? ''
  // The re-dispatch entry point = the first CONTROLLABLE step's name (the rerun arm re-enters at a
  // registered seam). NO fallback to the first OBSERVED step: that name is not a controllable seam,
  // so the server's registry check would 422 the POST every time — a guaranteed-422 button is a
  // worse UX than no button. With no controllable step we render the empty/why state instead.
  const firstControllable = steps.find((s) => s.tier === 'controllable' && s.step_name)
  const fromStep = firstControllable?.step_name ?? null
  return (
    <div className="space-y-2">
      {reruns?.rerun_of_run_id && (
        <p data-rc-rerun-note className="text-xs text-muted-foreground bg-muted/40 border border-border rounded p-2">
          {RERUN_LINEAGE_COPY}{' '}
          <span className="font-mono text-muted-foreground">
            (from {reruns.rerun_of_run_id}
            {reruns.rerun_from_step ? ` @ ${reruns.rerun_from_step}` : ''})
          </span>
          {/* VT-375 C1 (Option A) binding disclosure — ships statically wherever lineage renders. */}
          <span data-rc-rerun-overlap-note className="mt-1 block italic text-muted-foreground">
            {RERUN_OVERLAP_COPY}
          </span>
        </p>
      )}

      {/* VT-376 rerun control — only on rerunnable runs; non-rerunnable show the why-copy. The
          fromStep is the first CONTROLLABLE seam. With none, there is no entry point the server
          would accept (a rerun from an observed step is a guaranteed 422), so we render the
          empty/why state instead of a button that would always fail (VT-381 nit). */}
      {fromStep ? (
        <div data-rc-run-rerun className="flex items-center gap-2">
          <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Re-dispatch:</span>
          <RerunControl
            runId={run.run_id}
            fromStep={fromStep}
            rerunnable={timeline.rerunnable}
            forbiddenReason={timeline.forbiddenReason}
          />
        </div>
      ) : timeline.rerunnable ? (
        <div data-rc-run-rerun className="flex items-center gap-2">
          <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Re-dispatch:</span>
          <span data-rc-rerun-no-entry className="text-[11px] italic text-muted-foreground">
            No controllable step to re-dispatch from — this run exposes no re-entry seam.
          </span>
        </div>
      ) : null}

      {steps.length === 0 ? (
        <p className="text-xs text-muted-foreground px-1 py-1">No steps recorded for this run.</p>
      ) : (
        <table className="min-w-full divide-y divide-border text-xs">
          <thead className="bg-muted/40">
            <tr>
              {['Seq', 'Step', 'Status', 'Duration', 'Override', 'Paused', 'Envelope keys', 'Control'].map(
                (h) => (
                  <th key={h} className="px-2 py-1 text-left font-medium uppercase text-muted-foreground">
                    {h}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {steps.map((s) => (
              <TimelineRow
                key={s.step_id ?? `${s.step_seq}`}
                step={s}
                tenantId={timeline.tenantId}
                workflowKind={workflowKind}
                runId={run.run_id}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function TimelineRow({
  step,
  tenantId,
  workflowKind,
  runId,
}: {
  step: VtrTimelineStep
  tenantId: string | null
  workflowKind: string
  runId: string
}) {
  // Control axis is orchestrator-authoritative: badge when the step is NOT controllable.
  // Absent/unknown tier ⇒ treat as observed (fail-safe — never imply controllable on silence).
  const observed = step.tier !== 'controllable'
  return (
    <tr data-testid="rc-timeline-row" className="hover:bg-muted/40">
      <td className="px-2 py-1 text-muted-foreground font-mono">{step.step_seq}</td>
      <td className="px-2 py-1">
        <span className="font-mono text-foreground">
          {step.step_kind ?? '—'}
          {step.step_name ? `:${step.step_name}` : ''}
        </span>
        {observed && (
          <span
            data-rc-observed-badge
            className="ml-1 inline-block rounded border border-border bg-muted px-1 py-0.5 text-[10px] font-medium text-muted-foreground"
            title="Observed-tier step — timeline display only; not controllable in this panel."
          >
            {OBSERVED_BADGE_COPY}
          </span>
        )}
      </td>
      <td className="px-2 py-1">
        <StatusPill status={step.step_status} />
      </td>
      <td className="px-2 py-1 text-muted-foreground">{fmtDuration(step.duration_ms)}</td>
      <td className="px-2 py-1 font-mono text-muted-foreground">{step.override_id ?? '—'}</td>
      <td className="px-2 py-1 text-muted-foreground">{step.paused_ms != null ? fmtDuration(step.paused_ms) : '—'}</td>
      <td className="px-2 py-1 text-muted-foreground">
        <span className="font-mono">{envelopeKeys(step.input_envelope)}</span>
        <span className="ml-1 block text-[10px] text-muted-foreground" title={ENVELOPE_KEYS_COPY}>
          {ENVELOPE_KEYS_COPY}
        </span>
      </td>
      {/* VT-376: override control ONLY on controllable steps. Observed steps render NO control
          (the OverrideControl returns null), keeping the honesty contract — observed = no buttons. */}
      <td className="px-2 py-1">
        {observed || !tenantId ? (
          <span className="text-[10px] text-muted-foreground/60">—</span>
        ) : (
          <OverrideControl
            step={step}
            workflowKind={workflowKind}
            tenantId={tenantId}
            workflowId={runId}
          />
        )}
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
    <div data-testid="rc-program-tile" className="rounded border border-border bg-card">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-2 text-left hover:bg-muted/40"
        aria-expanded={open}
      >
        <span className="flex items-center gap-2">
          <span className="font-mono text-xs text-foreground">{run.run_id}</span>
          {run.run_type && <span className="text-xs text-muted-foreground">{run.run_type}</span>}
          <StatusPill status={run.status} />
          {run.active_hold && (
            <span className="rounded border border-gold/40 bg-gold/15 px-1.5 py-0.5 text-[10px] font-medium text-gold-foreground">
              held
            </span>
          )}
          {run.rerun_of_run_id && (
            <span className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
              re-dispatched
            </span>
          )}
          {/* C1 outcome rendering — run rows carry status 'escalated' on the overlap close
              (metadata stays server-side); the marker + title disclose the why. */}
          {run.rerun_of_run_id && run.status === 'escalated' && (
            <span
              data-rc-rerun-escalated
              className="rounded border border-destructive/30 bg-destructive/10 px-1.5 py-0.5 text-[10px] font-medium text-destructive"
              title={RERUN_OVERLAP_COPY}
            >
              escalated re-run
            </span>
          )}
        </span>
        <span className="text-xs text-muted-foreground">
          {fmtTs(run.started_at)} · {run.step_count ?? '—'} steps · {open ? 'hide' : 'show'}
        </span>
      </button>
      {open && (
        <div className="border-t border-border px-3 py-2">
          <StepTimeline timeline={timeline} run={run} />
        </div>
      )}
    </div>
  )
}

/** Pause/release tiles for every pauseable kind for one tenant. A kind currently held shows
 *  Release; otherwise Pause. Held state is read from the programs holds projection. */
function PauseTiles({ tenantId, holds }: { tenantId: string; holds: VtrHold[] }) {
  const heldKinds = new Set(holds.map((h) => h.workflow_kind))
  return (
    <div data-rc-pause-tiles className="space-y-1.5">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Pause / release (per workflow kind)
      </h4>
      <div className="flex flex-wrap gap-2">
        {PAUSEABLE_KINDS.map((kind) => (
          <span
            key={kind}
            data-rc-pause-tile={kind}
            className="inline-flex items-center gap-2 rounded border border-border bg-card px-2 py-1 text-xs"
          >
            <span className="font-mono text-muted-foreground">{kind}</span>
            <PauseReleaseControls
              tenantId={tenantId}
              workflowKind={kind}
              paused={heldKinds.has(kind)}
            />
          </span>
        ))}
      </div>
    </div>
  )
}

function UpcomingTile({ item }: { item: VtrUpcomingItem }) {
  return (
    <div data-testid="rc-program-tile" className="rounded border border-border bg-card px-3 py-2">
      <div className="flex items-center justify-between">
        <span className="text-sm text-foreground">{item.label}</span>
        <span className="rounded border border-border bg-muted/40 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
          {item.kind}
        </span>
      </div>
      <div className="mt-0.5 flex items-center justify-between text-[11px] text-muted-foreground">
        <span>{fmtTs(item.due_at)}</span>
        <span className="font-mono">{item.source}</span>
      </div>
    </div>
  )
}

function HoldsList({ holds }: { holds: VtrHold[] }) {
  if (holds.length === 0) return null
  return (
    <div className="rounded border border-gold/40 bg-gold/15 p-2">
      <p className="text-xs font-medium text-gold-foreground">Active holds</p>
      <ul className="mt-1 space-y-0.5">
        {holds.map((h, i) => (
          <li key={`${h.workflow_kind}-${i}`} className="text-xs text-gold-foreground/90">
            <span className="font-mono">{h.workflow_kind}</span> · held since {fmtTs(h.set_at)}
          </li>
        ))}
      </ul>
      <p data-rc-holds-footer className="mt-1 text-[11px] italic text-gold-foreground/80">
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
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h4>
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
      className="rounded-lg border border-border bg-card shadow-sm"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-muted/40"
        aria-expanded={open}
      >
        <span className="flex items-center gap-2">
          <span className="text-sm font-medium text-foreground">
            {data.tenantName ?? data.tenantId}
          </span>
          <span className="font-mono text-[11px] text-muted-foreground">{data.tenantId}</span>
        </span>
        <span className="flex items-center gap-2 text-xs text-muted-foreground">
          <span>{programs.running.length} running</span>
          <span>·</span>
          <span>{programs.past.length} past</span>
          <span>·</span>
          <span>{programs.upcoming7d.length} upcoming</span>
          {programs.holds.length > 0 && (
            <span className="rounded border border-gold/40 bg-gold/15 px-1.5 py-0.5 font-medium text-gold-foreground">
              {programs.holds.length} held
            </span>
          )}
          <span className="text-muted-foreground">{open ? 'collapse' : 'expand'}</span>
        </span>
      </button>

      {open && (
        <div className="space-y-3 border-t border-border p-4">
          {programs.degraded && (
            <p
              data-testid="rc-degraded-banner"
              className="rounded border border-gold/40 bg-gold/15 p-2 text-xs font-medium text-gold-foreground"
            >
              {DEGRADED_COPY}
            </p>
          )}

          <HoldsList holds={programs.holds} />

          {/* VT-376 pause/release controls — tenant × kind tiles. */}
          <PauseTiles tenantId={data.tenantId} holds={programs.holds} />

          <ProgramGroup title="Running">
            {programs.running.length === 0 ? (
              <p className="text-xs text-muted-foreground">No running programs.</p>
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
              <p className="text-xs text-muted-foreground">No past programs.</p>
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
              <p className="text-xs text-muted-foreground">Nothing forecast in the next 7 days.</p>
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
