'use client'

/**
 * VT-294 — Behaviour & Training (client). Decision metrics + recent-decisions listing with
 * an inline "Train" (records corrective feedback → ops_audit). Drill into a decision via the
 * VT-290 overlay. Scope label reflects own (VTR) vs all (VTAdmin).
 */

import { useState, useTransition } from 'react'

import { useOverlay } from '@/components/ops/overlay-context'
import { trainAction } from '@/app/(app)/team/ops/behaviour/actions'
import type { DecisionMetrics, DecisionRow } from '@/lib/ops/behaviour'

export function BehaviourPanel({ metrics, decisions }: { metrics: DecisionMetrics; decisions: DecisionRow[] }) {
  const overlay = useOverlay()
  const [pending, start] = useTransition()
  const [flash, setFlash] = useState<Record<string, string>>({})

  function train(d: DecisionRow, note: string) {
    start(async () => {
      const res = await trainAction(d.id, note) // owner resolved server-side (no IDOR)
      setFlash((f) => ({ ...f, [d.id]: res.ok ? 'trained' : `failed: ${res.reason ?? '?'}` }))
    })
  }

  return (
    <section data-ops-behaviour>
      <h2>Decision metrics ({metrics.scope === 'own' ? 'your decisions' : 'all operators'}, 30d)</h2>
      <p data-ops-decision-total>Total decisions: {metrics.total}</p>
      <ul data-ops-decision-by-action>
        {Object.entries(metrics.byAction).map(([action, n]) => (
          <li key={action}>{action}: {n}</li>
        ))}
      </ul>

      <h3>Recent decisions</h3>
      {decisions.length === 0 ? (
        <p data-ops-empty>No decisions recorded yet.</p>
      ) : (
        <table data-ops-decisions>
          <thead>
            <tr><th>Action</th><th>Target</th><th>When</th><th /></tr>
          </thead>
          <tbody>
            {decisions.map((d) => (
              <tr key={d.id}>
                <td>{d.action}</td>
                <td>{d.target_kind}{d.target_id ? ` ${d.target_id.slice(0, 8)}` : ''}</td>
                <td>{d.created_at}</td>
                <td>
                  <button
                    type="button"
                    disabled={pending || !!flash[d.id]}
                    onClick={() => train(d, 'flagged for training')}
                  >
                    {flash[d.id] ?? 'Train'}
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      overlay.open({
                        key: `decision-${d.id}`,
                        title: `Decision ${d.id.slice(0, 8)}`,
                        content: (
                          <ul>
                            <li>Operator: {d.operator_id.slice(0, 8)}</li>
                            <li>Action: {d.action}</li>
                            <li>Target: {d.target_kind} {d.target_id ?? ''}</li>
                            <li>When: {d.created_at}</li>
                          </ul>
                        ),
                      })
                    }
                  >
                    Open
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
