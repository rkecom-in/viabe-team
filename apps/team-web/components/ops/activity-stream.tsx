'use client'

/**
 * VT-293 — Activity listing (client). Per-agent runs; "Stream" drills into the VT-290
 * overlay with the timestamped step trace (fetched on open). Inline Escalate (real → VT-292
 * escalation) + Pause/Steer/Override (record an ops_audit control intent — orchestrator
 * run-control wiring is the follow-up; the request is logged, never a dead-end).
 */

import { useState, useTransition } from 'react'

import { useOverlay } from '@/components/ops/overlay-context'
import {
  OpsTable,
  OpsEmpty,
  OpsChip,
  OpsMono,
  opsCellClass,
  opsButtonClass,
  statusTone,
} from '@/components/ops/ops-ui'
import {
  escalateRunAction,
  fetchRunStepsAction,
  flagRunControlAction,
} from '@/app/(app)/team/ops/activity/actions'
import type { ActivityRun, StepRow } from '@/lib/ops/activity'

function StreamView({ runId }: { runId: string }) {
  const [steps, setSteps] = useState<StepRow[] | null>(null)
  const [loading, start] = useTransition()
  if (steps === null && !loading) {
    start(async () => setSteps(await fetchRunStepsAction(runId)))
  }
  if (loading || steps === null) return <p className="text-sm text-gray-500">Loading stream…</p>
  if (steps.length === 0)
    return <p className="text-sm text-gray-500">No steps (or not authorized).</p>
  return (
    <ol data-ops-stream className="space-y-2">
      {steps.map((s) => (
        <li
          key={s.step_index}
          className="rounded-md border border-gray-200 bg-white px-3 py-2 text-sm text-gray-700"
        >
          <div className="flex flex-wrap items-center gap-2">
            <OpsMono>#{s.step_index}</OpsMono>
            <span className="font-medium text-gray-900">{s.step_kind}</span>
            <OpsChip tone={statusTone(s.status)}>{s.status}</OpsChip>
            {s.duration_ms != null ? (
              <span className="text-xs tabular-nums text-gray-400">{s.duration_ms}ms</span>
            ) : null}
          </div>
          {s.rationale ? (
            <div data-ops-rationale className="mt-1.5 text-xs leading-relaxed text-gray-500">
              {s.rationale}
            </div>
          ) : null}
        </li>
      ))}
    </ol>
  )
}

export function ActivityStream({ runs }: { runs: ActivityRun[] }) {
  const overlay = useOverlay()
  const [pending, start] = useTransition()
  const [flash, setFlash] = useState<Record<string, string>>({})

  if (runs.length === 0) return <OpsEmpty data-ops-empty>No recent activity.</OpsEmpty>

  function escalate(r: ActivityRun) {
    start(async () => {
      const res = await escalateRunAction(r.run_id) // tenant resolved server-side (no IDOR)
      setFlash((f) => ({ ...f, [r.run_id]: res.ok ? 'escalated' : `failed: ${res.reason ?? '?'}` }))
    })
  }
  function control(r: ActivityRun, kind: string) {
    start(async () => {
      const res = await flagRunControlAction(r.run_id, kind)
      setFlash((f) => ({ ...f, [r.run_id]: res.ok ? `${kind} requested` : `failed: ${res.reason ?? '?'}` }))
    })
  }

  return (
    <OpsTable tableProps={{ 'data-ops-activity': '' }} headers={['Run', 'Status', 'Started', 'Actions']}>
      {runs.map((r) => {
        const statusText = flash[r.run_id] ?? r.status
        return (
          <tr key={r.run_id} data-status={r.status} className="hover:bg-gray-50">
            <td className={opsCellClass}>
              <OpsMono>{r.run_id.slice(0, 8)}</OpsMono>
            </td>
            <td className={opsCellClass}>
              <OpsChip tone={statusTone(statusText)}>{statusText}</OpsChip>
            </td>
            <td className={`${opsCellClass} whitespace-nowrap text-gray-500`}>{r.started_at}</td>
            <td className={opsCellClass}>
              <div className="flex flex-wrap items-center gap-1.5">
                <button
                  type="button"
                  className={opsButtonClass('ghost')}
                  onClick={() =>
                    overlay.open({
                      key: `stream-${r.run_id}`,
                      title: `Run ${r.run_id.slice(0, 8)}`,
                      content: <StreamView runId={r.run_id} />,
                    })
                  }
                >
                  Stream
                </button>
                <button
                  type="button"
                  className={opsButtonClass('primary')}
                  disabled={pending}
                  onClick={() => escalate(r)}
                >
                  Escalate
                </button>
                {/* VT-374 (N1 retire): 'pause' sets a tenant-wide campaign_send hold on the
                    run-control substrate — it stays until released via the run-control API
                    (no auto-expiry). 'steer' moved to the Run-Control panel (Phase B): this
                    legacy button now returns a "moved to the Run-Control panel" message. */}
                <button
                  type="button"
                  className={opsButtonClass()}
                  disabled={pending}
                  onClick={() => control(r, 'pause')}
                  title="Sets a tenant-wide campaign_send hold; released via the run-control API (POST …/run-control/release)"
                >
                  Pause campaign sends
                </button>
                <button
                  type="button"
                  className={opsButtonClass()}
                  disabled={pending}
                  onClick={() => control(r, 'steer')}
                  title="Steer moved to the Run-Control panel (Phase B)"
                >
                  Steer (moved →)
                </button>
              </div>
            </td>
          </tr>
        )
      })}
    </OpsTable>
  )
}
