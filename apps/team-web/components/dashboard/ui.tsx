/**
 * VT-372 — shared owner-dashboard UI primitives.
 *
 * Server components (no client JS). They reuse the marketing surface's design language
 * (light theme, emerald accent, rounded-2xl cards, gray scale, max-w containers) so the
 * dashboard reads as the same product. Light-mode only (artifact lock).
 */
import type { ReactNode } from 'react'

/** A page heading block: title + optional subtitle, consistent across every dashboard page. */
export function PageHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-6">
      <h1 className="text-2xl font-bold tracking-tight text-gray-900 sm:text-3xl">{title}</h1>
      {subtitle ? <p className="mt-1.5 text-sm leading-relaxed text-gray-500">{subtitle}</p> : null}
    </div>
  )
}

/** A surface card — the marketing `rounded-2xl border border-gray-200 bg-white shadow-sm`. */
export function Card({
  children,
  className = '',
  label,
}: {
  children: ReactNode
  className?: string
  label?: string
}) {
  return (
    <section
      aria-label={label}
      className={`rounded-2xl border border-gray-200 bg-white p-5 shadow-sm sm:p-6 ${className}`}
    >
      {children}
    </section>
  )
}

/** A section heading inside a card. */
export function CardTitle({ children }: { children: ReactNode }) {
  return <h2 className="text-base font-semibold tracking-tight text-gray-900">{children}</h2>
}

/** A headline metric tile (e.g. customer count). */
export function MetricTile({
  value,
  label,
  testid,
}: {
  value: ReactNode
  label: string
  testid?: string
}) {
  return (
    <div className="rounded-2xl border border-gray-200 bg-white p-5 shadow-sm sm:p-6">
      <div
        data-testid={testid}
        className="text-3xl font-extrabold tracking-tight text-gray-900 sm:text-4xl"
      >
        {value}
      </div>
      <div className="mt-1 text-sm text-gray-500">{label}</div>
    </div>
  )
}

/** A coloured status pill (emerald / amber / gray) — derives tone from the status string. */
export function StatusChip({ status, unknownLabel }: { status: string | null; unknownLabel: string }) {
  const s = (status ?? '').toLowerCase()
  const tone =
    s.includes('sent') || s.includes('done') || s.includes('complete') || s.includes('active')
      ? 'bg-emerald-50 text-emerald-700 ring-emerald-600/20'
      : s.includes('pending') || s.includes('queue') || s.includes('draft') || s.includes('progress')
        ? 'bg-amber-50 text-amber-700 ring-amber-600/20'
        : s.includes('opt') || s.includes('exclud') || s.includes('fail') || s.includes('error')
          ? 'bg-rose-50 text-rose-700 ring-rose-600/20'
          : 'bg-gray-100 text-gray-600 ring-gray-500/20'
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium capitalize ring-1 ring-inset ${tone}`}
    >
      {status ?? unknownLabel}
    </span>
  )
}

/**
 * A responsive data table. Wraps in a scroll container + card chrome so long tables stay
 * usable on mobile. `headers` are right-aligned when flagged (numeric columns).
 */
export function DataTable({
  headers,
  children,
}: {
  headers: { label: string; align?: 'right' }[]
  children: ReactNode
}) {
  return (
    <div className="overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-sm">
      <div className="overflow-x-auto">
        <table className="w-full min-w-full text-left text-sm">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50/80">
              {headers.map((h) => (
                <th
                  key={h.label}
                  scope="col"
                  className={`px-4 py-3 text-xs font-semibold uppercase tracking-wide text-gray-500 ${
                    h.align === 'right' ? 'text-right' : ''
                  }`}
                >
                  {h.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">{children}</tbody>
        </table>
      </div>
    </div>
  )
}

/** A styled empty-state inside a table/list region. */
export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-2xl border border-dashed border-gray-300 bg-gray-50/60 px-6 py-12 text-center text-sm text-gray-500">
      {children}
    </div>
  )
}

/** The styled load-error fallback shared by every page (replaces the bare <p>). */
export function LoadError({ title, message }: { title: string; message: string }) {
  return (
    <div className="mx-auto w-full max-w-2xl">
      <PageHeader title={title} />
      <div className="rounded-2xl border border-rose-200 bg-rose-50 px-6 py-10 text-center">
        <p className="text-sm text-rose-700">{message}</p>
      </div>
    </div>
  )
}

/**
 * A full-page styled "coming soon" empty-state for the not-yet-built sub-pages
 * (launch / sessions / sprints) — a labelled card with intent, not a bare h1.
 */
export function ComingSoon({
  title,
  headline,
  body,
  badge,
}: {
  title: string
  headline: string
  body: string
  badge: string
}) {
  return (
    <div className="mx-auto w-full max-w-2xl">
      <PageHeader title={title} />
      <div className="rounded-2xl border border-gray-200 bg-white px-6 py-14 text-center shadow-sm">
        <span className="inline-flex items-center rounded-full bg-emerald-50 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-emerald-700 ring-1 ring-inset ring-emerald-600/20">
          {badge}
        </span>
        <h2 className="mt-5 text-lg font-semibold tracking-tight text-gray-900">{headline}</h2>
        <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-gray-500">{body}</p>
      </div>
    </div>
  )
}
