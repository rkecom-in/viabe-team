'use client'

/**
 * VT-296 — Monitoring / Watchdog board (client, read-only). Watchdog detector firings
 * (crash / stall / misbehaviour) per business, severity-sorted. "Open" drills into the
 * VT-290 overlay for the offending run — never a dead end. De-identified rows (CL-426)
 * come from the server (VTR sees no message_text).
 */

import { useOverlay } from '@/components/ops/overlay-context'
import {
  OpsTable,
  OpsEmpty,
  OpsChip,
  OpsMono,
  opsCellClass,
  opsButtonClass,
  severityTone,
} from '@/components/ops/ops-ui'
import type { MonitoringItem } from '@/lib/ops/monitoring'

const CATEGORY_LABEL: Record<MonitoringItem['category'], string> = {
  crash: 'Crash',
  stall: 'Stall',
  misbehaviour: 'Misbehaviour',
}

const CATEGORY_TONE: Record<MonitoringItem['category'], 'red' | 'amber' | 'gray'> = {
  crash: 'red',
  stall: 'amber',
  misbehaviour: 'gray',
}

export function MonitoringBoard({ items }: { items: MonitoringItem[] }) {
  const overlay = useOverlay()

  if (items.length === 0)
    return <OpsEmpty data-ops-empty>No watchdog signals in the last 24h.</OpsEmpty>

  return (
    <OpsTable
      tableProps={{ 'data-ops-monitoring': '' }}
      headers={['Business', 'Category', 'Detector', 'Severity', 'When', 'Run']}
    >
      {items.map((it) => (
        <tr
          key={it.id}
          data-severity={it.severity}
          data-category={it.category}
          className="hover:bg-gray-50"
        >
          <td className={`${opsCellClass} font-medium text-gray-900`}>
            {it.tenant_name ?? it.reference}
          </td>
          <td className={opsCellClass}>
            <OpsChip tone={CATEGORY_TONE[it.category]}>{CATEGORY_LABEL[it.category]}</OpsChip>
          </td>
          <td className={`${opsCellClass} text-gray-700`}>{it.kind}</td>
          <td className={opsCellClass}>
            <OpsChip tone={severityTone(it.severity)}>{it.severity}</OpsChip>
          </td>
          <td className={`${opsCellClass} whitespace-nowrap text-gray-500`}>{it.time}</td>
          <td className={opsCellClass}>
            {it.run_id ? (
              <button
                type="button"
                className={opsButtonClass()}
                onClick={() =>
                  overlay.open({
                    key: `mon-${it.id}`,
                    title: `${CATEGORY_LABEL[it.category]} — ${it.tenant_name ?? it.reference}`,
                    content: (
                      <div data-ops-monitoring-detail className="space-y-4 text-sm text-gray-700">
                        <dl className="grid grid-cols-[6rem_1fr] gap-x-3 gap-y-2">
                          <dt className="text-gray-500">Category</dt>
                          <dd>
                            <OpsChip tone={CATEGORY_TONE[it.category]}>
                              {CATEGORY_LABEL[it.category]}
                            </OpsChip>
                          </dd>
                          <dt className="text-gray-500">Detector</dt>
                          <dd className="text-gray-900">{it.kind}</dd>
                          <dt className="text-gray-500">Severity</dt>
                          <dd>
                            <OpsChip tone={severityTone(it.severity)}>{it.severity}</OpsChip>
                          </dd>
                          <dt className="text-gray-500">When</dt>
                          <dd className="text-gray-900">{it.time}</dd>
                          <dt className="text-gray-500">Run</dt>
                          <dd>
                            <OpsMono>{it.run_id}</OpsMono>
                          </dd>
                          {it.message_text && (
                            <>
                              <dt className="text-gray-500">Detail</dt>
                              <dd className="text-gray-900">{it.message_text}</dd>
                            </>
                          )}
                        </dl>
                        <a
                          href={`/team/ops/activity?run=${it.run_id}`}
                          className="inline-flex text-sm font-medium text-blue-600 hover:text-blue-800"
                        >
                          Open in Activity →
                        </a>
                      </div>
                    ),
                  })
                }
              >
                Open
              </button>
            ) : (
              <span data-ops-no-run className="text-gray-400">
                —
              </span>
            )}
          </td>
        </tr>
      ))}
    </OpsTable>
  )
}
