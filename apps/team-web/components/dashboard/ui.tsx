/**
 * VT-372 — shared owner-dashboard UI primitives.
 *
 * Server components (no client JS). They reuse the marketing surface's design language
 * (light theme, Viabe brand tokens — saffron primary / Deep Green secondary, rounded-2xl
 * cards, semantic muted/border tokens, max-w containers) so the dashboard reads as the
 * same product. Light-mode only (artifact lock).
 */
import type { ReactNode } from 'react'

/** A page heading block: title + optional subtitle, consistent across every dashboard page. */
export function PageHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-6">
      <h1 className="text-2xl font-bold tracking-tight text-foreground sm:text-3xl">{title}</h1>
      {subtitle ? <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{subtitle}</p> : null}
    </div>
  )
}

/** A surface card — the marketing `rounded-2xl border border-border bg-card shadow-sm`. */
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
      className={`rounded-2xl border border-border bg-card p-5 shadow-sm sm:p-6 ${className}`}
    >
      {children}
    </section>
  )
}

/** A section heading inside a card. */
export function CardTitle({ children }: { children: ReactNode }) {
  return <h2 className="text-base font-semibold tracking-tight text-foreground">{children}</h2>
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
    <div className="rounded-2xl border border-border bg-card p-5 shadow-sm sm:p-6">
      <div
        data-testid={testid}
        className="text-3xl font-extrabold tracking-tight text-foreground sm:text-4xl"
      >
        {value}
      </div>
      <div className="mt-1 text-sm text-muted-foreground">{label}</div>
    </div>
  )
}

/** A coloured status pill (green / gold / destructive / neutral) — derives tone from the status string. */
export function StatusChip({ status, unknownLabel }: { status: string | null; unknownLabel: string }) {
  const s = (status ?? '').toLowerCase()
  const tone =
    s.includes('sent') || s.includes('done') || s.includes('complete') || s.includes('active')
      ? 'bg-secondary/10 text-secondary ring-secondary/20'
      : s.includes('pending') || s.includes('queue') || s.includes('draft') || s.includes('progress')
        ? 'bg-gold/15 text-gold-foreground ring-gold/30'
        : s.includes('opt') || s.includes('exclud') || s.includes('fail') || s.includes('error')
          ? 'bg-destructive/10 text-destructive ring-destructive/20'
          : 'bg-muted text-muted-foreground ring-border'
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
    <div className="overflow-hidden rounded-2xl border border-border bg-card shadow-sm">
      <div className="overflow-x-auto">
        <table className="w-full min-w-full text-left text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/40">
              {headers.map((h) => (
                <th
                  key={h.label}
                  scope="col"
                  className={`px-4 py-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground ${
                    h.align === 'right' ? 'text-right' : ''
                  }`}
                >
                  {h.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">{children}</tbody>
        </table>
      </div>
    </div>
  )
}

/** A styled empty-state inside a table/list region. */
export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-2xl border border-dashed border-input bg-muted/30 px-6 py-12 text-center text-sm text-muted-foreground">
      {children}
    </div>
  )
}

/** The styled load-error fallback shared by every page (replaces the bare <p>). */
export function LoadError({ title, message }: { title: string; message: string }) {
  return (
    <div className="mx-auto w-full max-w-2xl">
      <PageHeader title={title} />
      <div className="rounded-2xl border border-destructive/30 bg-destructive/10 px-6 py-10 text-center">
        <p className="text-sm text-destructive">{message}</p>
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
      <div className="rounded-2xl border border-border bg-card px-6 py-14 text-center shadow-sm">
        <span className="inline-flex items-center rounded-full bg-accent px-3 py-1 text-xs font-semibold uppercase tracking-wide text-accent-foreground ring-1 ring-inset ring-primary/20">
          {badge}
        </span>
        <h2 className="mt-5 text-lg font-semibold tracking-tight text-foreground">{headline}</h2>
        <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-muted-foreground">{body}</p>
      </div>
    </div>
  )
}
