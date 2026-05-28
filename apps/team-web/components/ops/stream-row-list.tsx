/**
 * VT-201 PR-2 — extracted row-rendering component used by BOTH the live
 * stream (PR-1's `StreamFeed`) and the history view (PR-2's
 * `StreamHistoryView`). Extracted so neither view duplicates row
 * formatting or data-col attribute keys; both ship the same DOM shape so
 * downstream consumers (Playwright selectors, screen-readers) work
 * uniformly.
 */

import type { PipelineStepEvent } from '@/lib/ops/stream'

export interface StreamRowListProps {
  rows: PipelineStepEvent[]
  tenantName: (tenantId: string) => string
}

export function StreamRowList({ rows, tenantName }: StreamRowListProps) {
  return (
    <ol data-section="stream-rows">
      {rows.map((step) => (
        <li
          key={step.id}
          data-step-id={step.id}
          data-step-kind={step.step_kind}
          data-step-status={step.status}
        >
          <span data-col="started_at">
            {new Date(step.started_at).toLocaleTimeString()}
          </span>
          <span data-col="tenant_name">{tenantName(step.tenant_id)}</span>
          <a data-col="run_id" href={`/team/ops/runs/${step.run_id}`}>
            {step.run_id.slice(0, 8)}
          </a>
          <span data-col="step_kind">{step.step_kind}</span>
          <span data-col="step_name">{step.step_name ?? '—'}</span>
          <span data-col="status">{step.status}</span>
          <span data-col="cost_paise">{step.cost_paise ?? 0}p</span>
          <span data-col="duration_ms">{step.duration_ms ?? '—'}ms</span>
        </li>
      ))}
    </ol>
  )
}
