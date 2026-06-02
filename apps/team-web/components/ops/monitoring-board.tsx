'use client'

/**
 * VT-296 — Monitoring / Watchdog board (client, read-only). Watchdog detector firings
 * (crash / stall / misbehaviour) per business, severity-sorted. "Open" drills into the
 * VT-290 overlay for the offending run — never a dead end. De-identified rows (CL-426)
 * come from the server (VTR sees no message_text).
 */

import { useOverlay } from '@/components/ops/overlay-context'
import type { MonitoringItem } from '@/lib/ops/monitoring'

const CATEGORY_LABEL: Record<MonitoringItem['category'], string> = {
  crash: 'Crash',
  stall: 'Stall',
  misbehaviour: 'Misbehaviour',
}

export function MonitoringBoard({ items }: { items: MonitoringItem[] }) {
  const overlay = useOverlay()

  if (items.length === 0) return <p data-ops-empty>No watchdog signals in the last 24h.</p>

  return (
    <table data-ops-monitoring>
      <thead>
        <tr>
          <th>Business</th>
          <th>Category</th>
          <th>Detector</th>
          <th>Severity</th>
          <th>When</th>
          <th>Run</th>
        </tr>
      </thead>
      <tbody>
        {items.map((it) => (
          <tr key={it.id} data-severity={it.severity} data-category={it.category}>
            <td>{it.tenant_name ?? it.reference}</td>
            <td>{CATEGORY_LABEL[it.category]}</td>
            <td>{it.kind}</td>
            <td>{it.severity}</td>
            <td>{it.time}</td>
            <td>
              {it.run_id ? (
                <button
                  type="button"
                  onClick={() =>
                    overlay.open({
                      key: `mon-${it.id}`,
                      title: `${CATEGORY_LABEL[it.category]} — ${it.tenant_name ?? it.reference}`,
                      content: (
                        <div data-ops-monitoring-detail>
                          <ul>
                            <li>Category: {CATEGORY_LABEL[it.category]}</li>
                            <li>Detector: {it.kind}</li>
                            <li>Severity: {it.severity}</li>
                            <li>When: {it.time}</li>
                            <li>Run: {it.run_id}</li>
                            {it.message_text && <li>Detail: {it.message_text}</li>}
                          </ul>
                          <p>
                            <a href={`/team/ops/activity?run=${it.run_id}`}>Open in Activity →</a>
                          </p>
                        </div>
                      ),
                    })
                  }
                >
                  Open
                </button>
              ) : (
                <span data-ops-no-run>—</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
