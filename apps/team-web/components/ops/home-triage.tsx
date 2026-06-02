/**
 * VT-290 — Home/Triage (urgency-first). Pure presentation; data from lib/ops/home.
 *
 * KPI tiles (each links to a real listing — nothing dead-ended) + an escalation snippet
 * (de-identified for VTR). The [More] link goes to the full Escalations listing (VT-292).
 */

import Link from 'next/link'

import type { HomeTriageData } from '@/lib/ops/home'

export function HomeTriage({ data }: { data: HomeTriageData }) {
  return (
    <section data-ops-home>
      <h2>Triage{data.scoped ? ' (your assigned businesses)' : ' (all businesses)'}</h2>

      <div data-ops-kpi-tiles style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap' }}>
        {data.kpis.map((tile) => (
          <Link
            key={tile.key}
            href={tile.href}
            data-ops-kpi-tile
            data-count={tile.count}
            style={{
              display: 'block',
              minWidth: 140,
              padding: '1rem',
              border: '1px solid #ddd',
              borderRadius: 8,
              textDecoration: 'none',
            }}
          >
            <div style={{ fontSize: '1.75rem', fontWeight: 700 }}>{tile.count}</div>
            <div>{tile.label}</div>
          </Link>
        ))}
      </div>

      <h3>Needs attention</h3>
      {data.escalations.length === 0 ? (
        <p data-ops-empty>Nothing needs attention right now.</p>
      ) : (
        <table data-ops-escalation-snippet>
          <thead>
            <tr>
              <th>Reference</th>
              <th>Kind</th>
              <th>Severity</th>
              <th>Time</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.escalations.map((row) => (
              <tr key={row.id}>
                <td>{row.reference}</td>
                <td>{row.kind}</td>
                <td>{row.severity}</td>
                <td>{row.time}</td>
                <td>{row.status}</td>
                <td>
                  <Link href={`/team/ops/escalations?focus=${row.id}`}>More →</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
