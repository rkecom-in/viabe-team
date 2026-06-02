'use client'

/**
 * VT-292 — Escalations listing (client). Inline Resolve / Ack actions (each calls the
 * server action → updates the row + appends ops_audit). "Open" drills into the VT-290
 * overlay for the row detail. De-identified rows (CL-426) come from the server.
 */

import { useState, useTransition } from 'react'

import { useOverlay } from '@/components/ops/overlay-context'
import { escalationAction } from '@/app/(app)/team/ops/escalations/actions'
import type { MaskedOpsRow } from '@/lib/ops/de-identify'

export function EscalationsList({ rows }: { rows: MaskedOpsRow[] }) {
  const overlay = useOverlay()
  const [pending, startTransition] = useTransition()
  const [done, setDone] = useState<Record<string, string>>({})

  if (rows.length === 0) return <p data-ops-empty>No open escalations.</p>

  function act(row: MaskedOpsRow, action: 'resolve' | 'ack') {
    startTransition(async () => {
      const res = await escalationAction(row.id, row.tenant_id, action)
      setDone((d) => ({ ...d, [row.id]: res.ok ? action : `failed: ${res.reason ?? '?'}` }))
    })
  }

  return (
    <table data-ops-escalations>
      <thead>
        <tr>
          <th>Reference</th>
          <th>Kind</th>
          <th>Severity</th>
          <th>Opened</th>
          <th>Status</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.id} data-severity={row.severity}>
            <td>{row.reference}</td>
            <td>{row.kind}</td>
            <td>{row.severity}</td>
            <td>{row.time}</td>
            <td>{done[row.id] ?? row.status}</td>
            <td>
              <button type="button" disabled={pending || !!done[row.id]} onClick={() => act(row, 'ack')}>
                Ack
              </button>
              <button type="button" disabled={pending || !!done[row.id]} onClick={() => act(row, 'resolve')}>
                Resolve
              </button>
              <button
                type="button"
                onClick={() =>
                  overlay.open({
                    key: `esc-${row.id}`,
                    title: `Escalation ${row.reference}`,
                    content: (
                      <div data-ops-escalation-detail>
                        <ul>
                          <li>Kind: {row.kind}</li>
                          <li>Severity: {row.severity}</li>
                          <li>Opened: {row.time}</li>
                          <li>Status: {done[row.id] ?? row.status}</li>
                        </ul>
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
