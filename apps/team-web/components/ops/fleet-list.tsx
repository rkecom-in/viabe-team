'use client'

/**
 * VT-291 — Fleet listing (client). Inline health per business; "Open" drills into a
 * right-drawer OVERLAY (the VT-290 primitive — no detail pages). The overlay shows the
 * tenant's health detail + a link to the full tenant view (nothing dead-ended).
 */

import Link from 'next/link'

import { useOverlay } from '@/components/ops/overlay-context'
import type { FleetRow } from '@/lib/ops/fleet'

const HEALTH_DOT: Record<string, string> = { green: '🟢', yellow: '🟡', red: '🔴' }

export function FleetList({ rows }: { rows: FleetRow[] }) {
  const overlay = useOverlay()
  if (rows.length === 0) return <p data-ops-empty>No agents in your fleet right now.</p>
  return (
    <table data-ops-fleet>
      <thead>
        <tr>
          <th>Health</th>
          <th>Business</th>
          <th>In-flight</th>
          <th>Escalated</th>
          <th>Hard limits</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.tenant_id} data-health={r.health}>
            <td>{HEALTH_DOT[r.health] ?? r.health}</td>
            <td>{r.tenant_name ?? r.tenant_id}</td>
            <td>{r.running}</td>
            <td>{r.escalated}</td>
            <td>{r.hard_limits}</td>
            <td>
              <button
                type="button"
                onClick={() =>
                  overlay.open({
                    key: `fleet-${r.tenant_id}`,
                    title: r.tenant_name ?? r.tenant_id,
                    content: (
                      <div data-ops-fleet-detail>
                        <p>Health: {HEALTH_DOT[r.health] ?? r.health} {r.health}</p>
                        <ul>
                          <li>In-flight: {r.running}</li>
                          <li>Escalated (24h): {r.escalated}</li>
                          <li>Hard limits (24h): {r.hard_limits}</li>
                        </ul>
                        <Link href={`/team/ops/tenants/${r.tenant_id}`}>Open full tenant view →</Link>
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
