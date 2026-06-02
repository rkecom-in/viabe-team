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

  if (rows.length === 0) return <p data-ops-empty>No businesses.</p>

  return (
    <table data-ops-assignment>
      <thead>
        <tr>
          <th>Business</th>
          <th>Assigned VTRs</th>
          <th>Assign</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {rows.map((b) => (
          <tr key={b.tenant_id}>
            <td>{b.business_name ?? shortId(b.tenant_id)}</td>
            <td>
              {b.assignments.length === 0 ? (
                <span data-ops-unassigned>unassigned</span>
              ) : (
                <ul>
                  {b.assignments.map((a) => (
                    <li key={a.assignment_id}>
                      {shortId(a.operator_id)}{' '}
                      <button
                        type="button"
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
            <td>
              <select
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
              <button type="button" disabled={pending} onClick={() => doAssign(b.tenant_id)}>
                Assign
              </button>
              {msg[b.tenant_id] && <span data-ops-msg> {msg[b.tenant_id]}</span>}
            </td>
            <td>
              <button
                type="button"
                onClick={() =>
                  overlay.open({
                    key: `assign-${b.tenant_id}`,
                    title: `Assignments — ${b.business_name ?? shortId(b.tenant_id)}`,
                    content: (
                      <div data-ops-assignment-detail>
                        <p>Business: {b.business_name ?? b.tenant_id}</p>
                        <ul>
                          {b.assignments.map((a) => (
                            <li key={a.assignment_id}>VTR {a.operator_id}</li>
                          ))}
                        </ul>
                        {b.assignments.length === 0 && <p>No active assignments.</p>}
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
      </tbody>
    </table>
  )
}
