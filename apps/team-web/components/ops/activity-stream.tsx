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
  if (loading || steps === null) return <p>Loading stream…</p>
  if (steps.length === 0) return <p>No steps (or not authorized).</p>
  return (
    <ol data-ops-stream>
      {steps.map((s) => (
        <li key={s.step_index}>
          <code>#{s.step_index}</code> {s.step_kind} — {s.status}
          {s.duration_ms != null ? ` (${s.duration_ms}ms)` : ''}
          {s.rationale ? <div data-ops-rationale>{s.rationale}</div> : null}
        </li>
      ))}
    </ol>
  )
}

export function ActivityStream({ runs }: { runs: ActivityRun[] }) {
  const overlay = useOverlay()
  const [pending, start] = useTransition()
  const [flash, setFlash] = useState<Record<string, string>>({})

  if (runs.length === 0) return <p data-ops-empty>No recent activity.</p>

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
    <table data-ops-activity>
      <thead>
        <tr><th>Run</th><th>Status</th><th>Started</th><th>Actions</th></tr>
      </thead>
      <tbody>
        {runs.map((r) => (
          <tr key={r.run_id} data-status={r.status}>
            <td><code>{r.run_id.slice(0, 8)}</code></td>
            <td>{flash[r.run_id] ?? r.status}</td>
            <td>{r.started_at}</td>
            <td>
              <button
                type="button"
                onClick={() =>
                  overlay.open({ key: `stream-${r.run_id}`, title: `Run ${r.run_id.slice(0, 8)}`, content: <StreamView runId={r.run_id} /> })
                }
              >
                Stream
              </button>
              <button type="button" disabled={pending} onClick={() => escalate(r)}>Escalate</button>
              {/* VT-293 honesty (Cowork): these RECORD a control intent (ops_audit); live
                  enforcement on the running agent is VT-300. Labels say "Request …". */}
              <button type="button" disabled={pending} onClick={() => control(r, 'pause')} title="Records a pause request (enforcement: VT-300)">Request pause</button>
              <button type="button" disabled={pending} onClick={() => control(r, 'steer')} title="Records a steer request (enforcement: VT-300)">Request steer</button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
