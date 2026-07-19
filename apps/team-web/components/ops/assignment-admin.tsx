'use client'

/**
 * VT-295 — Assignment management (VTAdmin, client). Lists every business + its active VTR
 * assignments; inline assign (pick an operator) / unassign. Each calls the server action
 * → validates + writes operator_assignments + appends ops_audit. "Open" drills into the
 * VT-290 overlay for the business's full assignment detail (no separate detail page).
 *
 * Operators are bare UUIDs (CL-390: no PII in the operator substrate). Reassignment takes
 * effect on the VTR's next request — surfaced inline as the row updates.
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
} from '@/components/ops/ops-ui'
import { assignAction, unassignAction } from '@/app/(app)/team/ops/assignment/actions'
import type {
  AssignableOperator,
  BusinessAssignment,
} from '@/lib/ops/assignment-admin'

function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id
}

export function AssignmentAdmin({
  businesses,
  operators,
}: {
  businesses: BusinessAssignment[]
  operators: AssignableOperator[]
}) {
  const overlay = useOverlay()
  const [pending, startTransition] = useTransition()
  const [rows, setRows] = useState<BusinessAssignment[]>(businesses)
  const [picks, setPicks] = useState<Record<string, string>>({})
  const [msg, setMsg] = useState<Record<string, string>>({})

  function refreshAfter(tenantId: string, mutate: (b: BusinessAssignment) => BusinessAssignment) {
    setRows((rs) => rs.map((b) => (b.tenant_id === tenantId ? mutate(b) : b)))
  }

  function doAssign(tenantId: string) {
    const operatorId = picks[tenantId]
    if (!operatorId) {
      setMsg((m) => ({ ...m, [tenantId]: 'pick an operator first' }))
      return
    }
    startTransition(async () => {
      const res = await assignAction(tenantId, operatorId)
      setMsg((m) => ({ ...m, [tenantId]: res.ok ? `assigned ${shortId(operatorId)}` : `failed: ${res.reason}` }))
    })
  }

  function doUnassign(tenantId: string, assignmentId: string, operatorId: string) {
    startTransition(async () => {
      const res = await unassignAction(assignmentId)
      if (res.ok) {
        refreshAfter(tenantId, (b) => ({
          ...b,
          assignments: b.assignments.filter((a) => a.assignment_id !== assignmentId),
        }))
        setMsg((m) => ({ ...m, [tenantId]: `unassigned ${shortId(operatorId)}` }))
      } else {
        setMsg((m) => ({ ...m, [tenantId]: `failed: ${res.reason}` }))
      }
    })
  }

  if (rows.length === 0) return <OpsEmpty data-ops-empty>No businesses.</OpsEmpty>

  return (
    <OpsTable
      tableProps={{ 'data-ops-assignment': '' }}
      headers={['Business', 'Assigned VTRs', 'Assign', '']}
    >
      {rows.map((b) => (
        <tr key={b.tenant_id} className="hover:bg-gray-50">
          <td className={`${opsCellClass} font-medium text-gray-900`}>
            {b.business_name ?? shortId(b.tenant_id)}
          </td>
          <td className={opsCellClass}>
            {b.assignments.length === 0 ? (
              <span data-ops-unassigned className="text-xs italic text-gray-400">
                unassigned
              </span>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {b.assignments.map((a) => (
                  <li key={a.assignment_id} className="flex items-center gap-2">
                    <OpsMono>{shortId(a.operator_id)}</OpsMono>
                    <button
                      type="button"
                      className={opsButtonClass('ghost')}
                      disabled={pending}
                      onClick={() => doUnassign(b.tenant_id, a.assignment_id, a.operator_id)}
                    >
                      Unassign
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </td>
          <td className={opsCellClass}>
            <div className="flex flex-wrap items-center gap-1.5">
              <select
                className="rounded-md border border-gray-300 bg-white px-2 py-1 text-xs text-gray-700 focus:border-gray-400 focus:outline-none"
                value={picks[b.tenant_id] ?? ''}
                onChange={(e) => setPicks((p) => ({ ...p, [b.tenant_id]: e.target.value }))}
              >
                <option value="">— operator —</option>
                {operators.map((o) => (
                  <option key={o.operator_id} value={o.operator_id}>
                    {shortId(o.operator_id)}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className={opsButtonClass('primary')}
                disabled={pending}
                onClick={() => doAssign(b.tenant_id)}
              >
                Assign
              </button>
              {msg[b.tenant_id] && (
                <span data-ops-msg className="text-xs text-gray-500">
                  {msg[b.tenant_id]}
                </span>
              )}
            </div>
          </td>
          <td className={`${opsCellClass} text-right`}>
            <button
              type="button"
              className={opsButtonClass()}
              onClick={() =>
                overlay.open({
                  key: `assign-${b.tenant_id}`,
                  title: `Assignments — ${b.business_name ?? shortId(b.tenant_id)}`,
                  content: (
                    <div data-ops-assignment-detail className="space-y-3 text-sm text-gray-700">
                      <p>
                        <span className="text-gray-500">Business: </span>
                        <span className="text-gray-900">{b.business_name ?? b.tenant_id}</span>
                      </p>
                      {b.assignments.length === 0 ? (
                        <p className="text-gray-500">No active assignments.</p>
                      ) : (
                        <ul className="space-y-1.5">
                          {b.assignments.map((a) => (
                            <li key={a.assignment_id} className="flex items-center gap-2">
                              <OpsChip tone="blue">VTR</OpsChip>
                              <OpsMono>{a.operator_id}</OpsMono>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  ),
                })
              }
            >
              Open
            </button>
          </td>
        </tr>
      ))}
    </OpsTable>
  )
}
