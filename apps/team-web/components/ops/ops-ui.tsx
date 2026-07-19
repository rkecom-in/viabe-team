/**
 * VT-412 — shared Ops Console UI primitives (styling pass).
 *
 * Server-safe presentational helpers (no client state) that give the ops data pages the
 * VT-405 tenant-profile quality bar: light theme, gray-50 surface, `rounded-lg border
 * border-gray-200 bg-white shadow-sm` cards, the same chip tones, a scroll-wrapped table
 * shell, styled empty / error states. Tailwind classNames only (Tailwind v4, no globals
 * layer). Light-mode only.
 *
 * These intentionally reuse the design language of `tenant-discovery-panel.tsx` (the VT-405
 * reference the ops console lands on) so the whole console reads as one product. Styling
 * only — they wrap existing markup and never change data, scope, or behaviour.
 */
import type { ReactNode } from 'react'

/** Page heading block — title + optional subtitle. Mirrors the dashboard PageHeader scale. */
export function OpsPageHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <header className="mb-1">
      <h1 className="text-2xl font-semibold tracking-tight text-gray-900">{title}</h1>
      {subtitle ? <p className="mt-1 text-sm text-gray-500">{subtitle}</p> : null}
    </header>
  )
}

/** A surface card matching the VT-405 reference (`rounded-lg border bg-white shadow-sm`). */
export function OpsCard({
  children,
  className = '',
  ...rest
}: {
  children: ReactNode
  className?: string
} & Record<`data-${string}`, string | undefined>) {
  return (
    <section
      className={`rounded-lg border border-gray-200 bg-white shadow-sm ${className}`}
      {...rest}
    >
      {children}
    </section>
  )
}

const CHIP_TONES = {
  gray: 'bg-gray-100 text-gray-700 ring-gray-500/20',
  blue: 'bg-blue-50 text-blue-700 ring-blue-600/20',
  green: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
  amber: 'bg-amber-50 text-amber-800 ring-amber-600/20',
  red: 'bg-rose-50 text-rose-700 ring-rose-600/20',
} as const

export type ChipTone = keyof typeof CHIP_TONES

/** A coloured status pill. Tone is passed explicitly (callers know their domain). */
export function OpsChip({
  children,
  tone = 'gray',
  className = '',
}: {
  children: ReactNode
  tone?: ChipTone
  className?: string
}) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium capitalize ring-1 ring-inset ${CHIP_TONES[tone]} ${className}`}
    >
      {children}
    </span>
  )
}

/** Maps a severity string to a chip tone (critical/high → red, medium → amber, else gray). */
export function severityTone(severity: string | null | undefined): ChipTone {
  const s = (severity ?? '').toLowerCase()
  if (s.includes('critical') || s.includes('high') || s.includes('sev1') || s.includes('p1')) return 'red'
  if (s.includes('medium') || s.includes('warn') || s.includes('sev2') || s.includes('p2')) return 'amber'
  return 'gray'
}

/** Maps a run / escalation status string to a chip tone. */
export function statusTone(status: string | null | undefined): ChipTone {
  const s = (status ?? '').toLowerCase()
  if (s.includes('resolv') || s.includes('done') || s.includes('complet') || s.includes('success') || s.includes('ack'))
    return 'green'
  if (s.includes('fail') || s.includes('error') || s.includes('abort') || s.includes('crash')) return 'red'
  if (s.includes('run') || s.includes('progress') || s.includes('pend') || s.includes('open') || s.includes('queue'))
    return 'amber'
  return 'gray'
}

/**
 * A table shell — the scroll-wrapped card chrome + styled <thead>. Callers keep their own
 * <tbody> + any data-* attrs on the <table>; this just supplies the chrome + column headers.
 */
export function OpsTable({
  headers,
  children,
  tableProps = {},
}: {
  headers: (string | { label: string; align?: 'right' | 'center' })[]
  children: ReactNode
  tableProps?: Record<string, string | undefined>
}) {
  return (
    <div className="overflow-hidden rounded-lg border border-gray-200 bg-white shadow-sm">
      <div className="overflow-x-auto">
        <table className="w-full min-w-full border-collapse text-left text-sm" {...tableProps}>
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50">
              {headers.map((h, i) => {
                const label = typeof h === 'string' ? h : h.label
                const align = typeof h === 'string' ? undefined : h.align
                return (
                  <th
                    key={`${label}-${i}`}
                    scope="col"
                    className={`px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-gray-500 ${
                      align === 'right' ? 'text-right' : align === 'center' ? 'text-center' : ''
                    }`}
                  >
                    {label}
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 text-gray-800">{children}</tbody>
        </table>
      </div>
    </div>
  )
}

/** Shared <td> padding/alignment so every cell lines up with the styled header. */
export const opsCellClass = 'px-4 py-2.5 align-middle'

/** A small action-bar button (Ack / Resolve / Open / Assign …). */
export function opsButtonClass(variant: 'default' | 'primary' | 'ghost' = 'default'): string {
  const base =
    'inline-flex items-center rounded-md px-2.5 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50'
  if (variant === 'primary')
    return `${base} bg-gray-900 text-white hover:bg-gray-700`
  if (variant === 'ghost')
    return `${base} text-gray-600 hover:bg-gray-100`
  return `${base} border border-gray-300 bg-white text-gray-700 hover:bg-gray-50`
}

/** A styled empty-state row inside a list/table region. Keeps the caller's data-* attr. */
export function OpsEmpty({
  children,
  ...rest
}: { children: ReactNode } & Record<`data-${string}`, string | undefined>) {
  return (
    <div
      className="rounded-lg border border-dashed border-gray-300 bg-gray-50 px-6 py-12 text-center text-sm text-gray-500"
      {...rest}
    >
      {children}
    </div>
  )
}

/** A styled load-error block (replaces the bare red <p>). Keeps the caller's data-* attr. */
export function OpsError({
  children,
  ...rest
}: { children: ReactNode } & Record<`data-${string}`, string | undefined>) {
  return (
    <div
      className="rounded-lg border border-rose-200 bg-rose-50 px-5 py-4 text-sm text-rose-700"
      {...rest}
    >
      {children}
    </div>
  )
}

/** Monospace short-id pill used across run/tenant references. */
export function OpsMono({ children }: { children: ReactNode }) {
  return (
    <code className="rounded bg-gray-100 px-1.5 py-0.5 font-mono text-xs text-gray-700">{children}</code>
  )
}
