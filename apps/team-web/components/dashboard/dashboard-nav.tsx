'use client'

/**
 * VT-372 — owner-dashboard navigation (sidebar on desktop, horizontal scroll-tabs on mobile).
 *
 * Was missing entirely (the layout rendered ONLY a language toggle, zero links to the
 * sub-pages). Links every dashboard view; the active route is highlighted via `usePathname`.
 * The `?lang` override is preserved on every link so the locale survives navigation.
 */
import Link from 'next/link'
import { usePathname, useSearchParams } from 'next/navigation'

export interface NavItem {
  href: string
  label: string
}

export function DashboardNav({ items, ariaLabel }: { items: NavItem[]; ariaLabel: string }) {
  const pathname = usePathname()
  const search = useSearchParams()
  const lang = search.get('lang')
  const qs = lang === 'hi' || lang === 'en' ? `?lang=${lang}` : ''

  const isActive = (href: string) =>
    href === '/team/dashboard' ? pathname === href : pathname === href || pathname.startsWith(`${href}/`)

  return (
    <nav
      aria-label={ariaLabel}
      className="flex gap-1 overflow-x-auto pb-1 sm:flex-col sm:gap-0.5 sm:overflow-visible sm:pb-0"
    >
      {items.map((item) => {
        const active = isActive(item.href)
        return (
          <Link
            key={item.href}
            href={`${item.href}${qs}`}
            aria-current={active ? 'page' : undefined}
            className={`whitespace-nowrap rounded-lg px-3 py-2 text-sm font-medium transition ${
              active
                ? 'bg-accent text-primary'
                : 'text-muted-foreground hover:bg-muted hover:text-foreground'
            }`}
          >
            {item.label}
          </Link>
        )
      })}
    </nav>
  )
}
