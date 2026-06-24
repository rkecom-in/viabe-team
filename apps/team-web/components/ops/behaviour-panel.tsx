'use client'

/**
 * VT-294 — Decision Audit (client). Decision metrics + recent-decisions listing with an inline
 * "Log feedback" (records operator feedback → ops_audit). Drill into a decision via the VT-290
 * overlay. Scope label reflects own (VTR) vs all (VTAdmin).
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
    <section data-ops-behaviour className="space-y-6">
      <div className="rounded-lg border border-gray-200 bg-white p-6 shadow-sm">
        <h2 className="text-lg font-semibold text-gray-900">
          Decision metrics ({metrics.scope === 'own' ? 'your decisions' : 'all operators'}, 30d)
        </h2>
        <p data-ops-decision-total className="mt-2 text-sm text-gray-700">
          Total decisions: <span className="font-medium text-gray-900">{metrics.total}</span>
        </p>
        <ul data-ops-decision-by-action className="mt-3 flex flex-wrap gap-2">
          {Object.entries(metrics.byAction).map(([action, n]) => (
            <li
              key={action}
              className="rounded-full bg-gray-100 px-3 py-1 text-xs font-medium text-gray-700"
            >
              {action}: {n}
            </li>
          ))}
        </ul>
      </div>

      <div className="rounded-lg border border-gray-200 bg-white p-6 shadow-sm">
        <h3 className="text-base font-semibold text-gray-900">Recent decisions</h3>
        {decisions.length === 0 ? (
          <p data-ops-empty className="mt-3 text-sm text-gray-600">
            No decisions recorded yet.
          </p>
        ) : (
          <div className="mt-3 overflow-x-auto">
            <table data-ops-decisions className="min-w-full text-left text-sm">
              <thead>
                <tr className="border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500">
                  <th className="px-3 py-2 font-medium">Action</th>
                  <th className="px-3 py-2 font-medium">Target</th>
                  <th className="px-3 py-2 font-medium">When</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {decisions.map((d) => (
                  <tr key={d.id} className="text-gray-700">
                    <td className="px-3 py-2">{d.action}</td>
                    <td className="px-3 py-2">
                      {d.target_kind}
                      {d.target_id ? ` ${d.target_id.slice(0, 8)}` : ''}
                    </td>
                    <td className="px-3 py-2 text-gray-500">{d.created_at}</td>
                    <td className="px-3 py-2">
                      <div className="flex gap-2">
                        <button
                          type="button"
                          disabled={pending || !!flash[d.id]}
                          onClick={() => train(d, 'flagged for training')}
                          className="rounded-md border border-gray-300 bg-white px-3 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {flash[d.id] ?? 'Log feedback'}
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
                          className="rounded-md border border-gray-300 bg-white px-3 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50"
                        >
                          Open
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  )
}
