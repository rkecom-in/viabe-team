/**
 * VT-290 — Ops Console V2 single-console side nav (role-gated).
 *
 * One console, all ops in it (binding rule). Nav items map to the VT-291..298 sub-rows.
 * Assignment is VTAdmin-only (VT-295). Server component — takes the resolved role; renders
 * the role badge + links. Pages not yet built (VT-291..298) link to placeholders that the
 * sub-rows fill in — never a dead end (the link target is a real, if stubbed, route).
 */

import Link from 'next/link'

import { OperatorRole, isVtAdmin } from '@/lib/auth/roles'

interface NavItem {
  href: string
  label: string
  vtAdminOnly?: boolean
  vt: string
}

const NAV: NavItem[] = [
  { href: '/team/ops', label: 'Home / Triage', vt: 'VT-290' },
  { href: '/team/ops/tenants', label: 'Tenants', vt: 'VT-412' },
  { href: '/team/ops/fleet', label: 'Fleet', vt: 'VT-291' },
  { href: '/team/ops/escalations', label: 'Escalations', vt: 'VT-292' },
  { href: '/team/ops/activity', label: 'Activity', vt: 'VT-293' },
  { href: '/team/ops/behaviour', label: 'Behaviour & Training', vt: 'VT-294' },
  { href: '/team/ops/assignment', label: 'Assignment', vtAdminOnly: true, vt: 'VT-295' },
  { href: '/team/ops/monitoring', label: 'Monitoring', vt: 'VT-296' },
  { href: '/team/ops/run-control', label: 'Run Control', vt: 'VT-375' },
  { href: '/team/ops/telegram', label: 'Connect Telegram', vt: 'VT-297' },
]

export function OpsSideNav({ role }: { role: OperatorRole }) {
  const admin = isVtAdmin(role)
  const items = NAV.filter((n) => !n.vtAdminOnly || admin)
  return (
    <nav data-ops-nav aria-label="Ops Console">
      <div data-ops-role-badge>{admin ? 'VTAdmin' : 'VTR'}</div>
      <ul>
        {items.map((n) => (
          <li key={n.href}>
            <Link href={n.href}>{n.label}</Link>
          </li>
        ))}
      </ul>
    </nav>
  )
}
