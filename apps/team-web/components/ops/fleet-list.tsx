'use client'

/**
 * VT-291 — Fleet listing (client). Inline health per business; "Open" drills into a
 * right-drawer OVERLAY (the VT-290 primitive — no detail pages). The overlay shows the
 * tenant's health detail + a link to the full tenant view (nothing dead-ended).
 */

import Link from 'next/link'

import { useOverlay } from '@/components/ops/overlay-context'
import { OpsTable, OpsEmpty, OpsChip, opsCellClass, opsButtonClass, type ChipTone } from '@/components/ops/ops-ui'
import type { FleetRow } from '@/lib/ops/fleet'

const HEALTH_DOT: Record<string, string> = { green: '🟢', yellow: '🟡', red: '🔴' }
const HEALTH_TONE: Record<string, ChipTone> = { green: 'green', yellow: 'amber', red: 'red' }

function HealthCell({ health }: { health: string }) {
  return (
    <OpsChip tone={HEALTH_TONE[health] ?? 'gray'}>
      <span aria-hidden className="mr-1">
        {HEALTH_DOT[health] ?? ''}
      </span>
      {health}
    </OpsChip>
  )
}

export function FleetList({ rows }: { rows: FleetRow[] }) {
  const overlay = useOverlay()
  if (rows.length === 0) return <OpsEmpty data-ops-empty>No agents in your fleet right now.</OpsEmpty>
  return (
    <OpsTable
      tableProps={{ 'data-ops-fleet': '' }}
      headers={[
        'Health',
        'Business',
        { label: 'In-flight', align: 'right' },
        { label: 'Escalated', align: 'right' },
        { label: 'Hard limits', align: 'right' },
        '',
      ]}
    >
      {rows.map((r) => (
        <tr key={r.tenant_id} data-health={r.health} className="hover:bg-gray-50">
          <td className={opsCellClass}>
            <HealthCell health={r.health} />
          </td>
          <td className={`${opsCellClass} font-medium text-gray-900`}>{r.tenant_name ?? r.tenant_id}</td>
          <td className={`${opsCellClass} text-right tabular-nums`}>{r.running}</td>
          <td className={`${opsCellClass} text-right tabular-nums`}>{r.escalated}</td>
          <td className={`${opsCellClass} text-right tabular-nums`}>{r.hard_limits}</td>
          <td className={`${opsCellClass} text-right`}>
            <button
              type="button"
              className={opsButtonClass()}
              onClick={() =>
                overlay.open({
                  key: `fleet-${r.tenant_id}`,
                  title: r.tenant_name ?? r.tenant_id,
                  content: (
                    <div data-ops-fleet-detail className="space-y-4 text-sm text-gray-700">
                      <div className="flex items-center gap-2">
                        <span className="text-gray-500">Health</span>
                        <HealthCell health={r.health} />
                      </div>
                      <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
                        <dt className="text-gray-500">In-flight</dt>
                        <dd className="text-right tabular-nums text-gray-900">{r.running}</dd>
                        <dt className="text-gray-500">Escalated (24h)</dt>
                        <dd className="text-right tabular-nums text-gray-900">{r.escalated}</dd>
                        <dt className="text-gray-500">Hard limits (24h)</dt>
                        <dd className="text-right tabular-nums text-gray-900">{r.hard_limits}</dd>
                      </dl>
                      <Link
                        href={`/team/ops/tenants/${r.tenant_id}`}
                        className="inline-flex text-sm font-medium text-blue-600 hover:text-blue-800"
                      >
                        Open full tenant view →
                      </Link>
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
