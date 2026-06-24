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
  { href: '/team/ops/behaviour', label: 'Decision Audit', vt: 'VT-294' },
  { href: '/team/ops/assignment', label: 'Assignment', vtAdminOnly: true, vt: 'VT-295' },
  { href: '/team/ops/monitoring', label: 'Monitoring', vt: 'VT-296' },
  { href: '/team/ops/run-control', label: 'Run Control', vt: 'VT-375' },
  { href: '/team/ops/telegram', label: 'Connect Telegram', vt: 'VT-297' },
]

export function OpsSideNav({ role }: { role: OperatorRole }) {
  const admin = isVtAdmin(role)
  const items = NAV.filter((n) => !n.vtAdminOnly || admin)
  return (
    <nav
      data-ops-nav
      aria-label="Ops Console"
      className="sticky top-0 hidden h-screen w-56 shrink-0 self-start overflow-y-auto border-r border-gray-200 bg-white px-3 py-5 sm:block"
    >
      <div className="px-2 pb-3">
        <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">Ops Console</div>
        <div
          data-ops-role-badge
          className={`mt-1.5 inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ring-1 ring-inset ${
            admin
              ? 'bg-emerald-50 text-emerald-700 ring-emerald-600/20'
              : 'bg-gray-100 text-gray-600 ring-gray-500/20'
          }`}
        >
          {admin ? 'VTAdmin' : 'VTR'}
        </div>
      </div>
      <ul className="flex flex-col gap-0.5">
        {items.map((n) => (
          <li key={n.href}>
            <Link
              href={n.href}
              className="block rounded-md px-3 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-100 hover:text-gray-900"
            >
              {n.label}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  )
}
