'use client'

/**
 * VT-292 — Escalations listing (client). Inline Resolve / Ack actions (each calls the
 * server action → updates the row + appends ops_audit). "Open" drills into the VT-290
 * overlay for the row detail. De-identified rows (CL-426) come from the server.
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
  severityTone,
  statusTone,
} from '@/components/ops/ops-ui'
import { escalationAction } from '@/app/(app)/team/ops/escalations/actions'
import type { MaskedOpsRow } from '@/lib/ops/de-identify'

export function EscalationsList({ rows }: { rows: MaskedOpsRow[] }) {
  const overlay = useOverlay()
  const [pending, startTransition] = useTransition()
  const [done, setDone] = useState<Record<string, string>>({})

  if (rows.length === 0) return <OpsEmpty data-ops-empty>No open escalations.</OpsEmpty>

  function act(row: MaskedOpsRow, action: 'resolve' | 'ack') {
    startTransition(async () => {
      const res = await escalationAction(row.id, row.tenant_id, action)
      setDone((d) => ({ ...d, [row.id]: res.ok ? action : `failed: ${res.reason ?? '?'}` }))
    })
  }

  return (
    <OpsTable
      tableProps={{ 'data-ops-escalations': '' }}
      headers={['Reference', 'Kind', 'Severity', 'Opened', 'Status', 'Actions']}
    >
      {rows.map((row) => {
        const statusText = done[row.id] ?? row.status
        return (
          <tr key={row.id} data-severity={row.severity} className="hover:bg-gray-50">
            <td className={opsCellClass}>
              <OpsMono>{row.reference}</OpsMono>
            </td>
            <td className={`${opsCellClass} text-gray-700`}>{row.kind}</td>
            <td className={opsCellClass}>
              <OpsChip tone={severityTone(row.severity)}>{row.severity}</OpsChip>
            </td>
            <td className={`${opsCellClass} whitespace-nowrap text-gray-500`}>{row.time}</td>
            <td className={opsCellClass}>
              <OpsChip tone={statusTone(statusText)}>{statusText}</OpsChip>
            </td>
            <td className={opsCellClass}>
              <div className="flex flex-wrap items-center gap-1.5">
                <button
                  type="button"
                  className={opsButtonClass()}
                  disabled={pending || !!done[row.id]}
                  onClick={() => act(row, 'ack')}
                >
                  Ack
                </button>
                <button
                  type="button"
                  className={opsButtonClass('primary')}
                  disabled={pending || !!done[row.id]}
                  onClick={() => act(row, 'resolve')}
                >
                  Resolve
                </button>
                <button
                  type="button"
                  className={opsButtonClass('ghost')}
                  onClick={() =>
                    overlay.open({
                      key: `esc-${row.id}`,
                      title: `Escalation ${row.reference}`,
                      content: (
                        <div data-ops-escalation-detail className="text-sm text-gray-700">
                          <dl className="grid grid-cols-[7rem_1fr] gap-x-3 gap-y-2">
                            <dt className="text-gray-500">Kind</dt>
                            <dd className="text-gray-900">{row.kind}</dd>
                            <dt className="text-gray-500">Severity</dt>
                            <dd>
                              <OpsChip tone={severityTone(row.severity)}>{row.severity}</OpsChip>
                            </dd>
                            <dt className="text-gray-500">Opened</dt>
                            <dd className="text-gray-900">{row.time}</dd>
                            <dt className="text-gray-500">Status</dt>
                            <dd>
                              <OpsChip tone={statusTone(statusText)}>{statusText}</OpsChip>
                            </dd>
                          </dl>
                        </div>
                      ),
                    })
                  }
                >
                  Open
                </button>
              </div>
            </td>
          </tr>
        )
      })}
    </OpsTable>
  )
}
