'use client'

/**
 * VT-713 — the client half of the tenant activity flow: latest-first stream in the
 * sim-artifact visual language (center rail, kind-chips, WhatsApp bubbles, severity
 * stripes) with LOAD-ON-SCROLL for arbitrarily long histories. An IntersectionObserver
 * sentinel at the bottom calls the server action for the next-older page; day pills are
 * `id`-anchored so the date hotlinks (#d-YYYY-MM-DD) land on them.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import type { FlowEvent, FlowPage } from '@/lib/ops/activity-flow'
import { loadOlderFlow } from '@/app/(app)/team/ops/tenants/[tenantId]/activity-flow/actions'

const KIND_CHIP: Record<FlowEvent['kind'], string> = {
  message: 'MESSAGE',
  decision: 'MANAGER DECISION',
  task: 'SUB-AGENT',
  step_error: 'EXECUTION ERROR',
  approval: 'APPROVAL',
  comms: 'COMMS',
  incident: 'INCIDENT',
  alert: 'ALERT',
}

const HOT_KINDS: ReadonlySet<FlowEvent['kind']> = new Set(['decision', 'task'])

const SEV_STRIPE: Record<FlowEvent['severity'], string> = {
  info: 'border-l-[#C9D4CC]',
  warn: 'border-l-[#B4560F]',
  error: 'border-l-[#c03434]',
}

const _URL_RE = /(https?:\/\/[^\s]+)/g

function Linkified({ text }: { text: string }) {
  const parts = text.split(_URL_RE)
  return (
    <>
      {parts.map((part, i) =>
        /^https?:\/\//.test(part) ? (
          <a
            key={i}
            href={part}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[#0E7A5F] underline decoration-[#9dbfae] underline-offset-2 hover:decoration-[#0E7A5F]"
          >
            {part}
          </a>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  )
}

function Chip({ e }: { e: FlowEvent }) {
  const hot = HOT_KINDS.has(e.kind) && e.severity === 'info'
  return (
    <span
      className={`absolute left-1/2 -translate-x-1/2 -top-2.5 z-10 rounded-full px-3 py-0.5 font-mono text-[10px] tracking-[0.06em] whitespace-nowrap ${
        hot ? 'bg-[#0E7A5F] text-white' : 'bg-[#EAEFE9] text-[#43554C]'
      }`}
    >
      {KIND_CHIP[e.kind]}
    </span>
  )
}

function SystemCard({ e }: { e: FlowEvent }) {
  const flagged = e.severity !== 'info'
  const meta = Object.entries(e.meta).filter(([, v]) => v)
  return (
    <div className="relative pt-3">
      <Chip e={e} />
      <div
        className={`relative z-[1] mx-auto w-full max-w-2xl rounded-md border border-[#DDE4DD] border-l-4 ${SEV_STRIPE[e.severity]} px-4 py-2.5 ${
          flagged ? 'bg-[#FBEFE3]' : 'bg-white'
        }`}
      >
        <div className="flex items-baseline justify-between gap-3">
          <p className="text-sm font-medium text-[#1C2B25]">{e.title}</p>
          <span className="font-mono text-[11px] text-[#93A399]">{String(e.ts).slice(11, 19)}Z</span>
        </div>
        {e.body ? (
          <p className="mt-1 text-xs whitespace-pre-wrap [overflow-wrap:anywhere] text-[#5C6B63]">
            <Linkified text={e.body} />
          </p>
        ) : null}
        {meta.length > 0 ? (
          <p className="mt-1 font-mono text-[10px] [overflow-wrap:anywhere] text-[#93A399]">
            {meta.map(([k, v]) => `${k}=${v}`).join('  ')}
          </p>
        ) : null}
      </div>
    </div>
  )
}

function MessageBubble({ e }: { e: FlowEvent }) {
  const owner = e.lane === 'owner'
  // VT-714 — pre-tenant turns (captured before the tenant existed) are visibly flagged.
  const preSignup = e.meta.surface === 'signup'
  return (
    <div className={`relative z-[1] flex ${owner ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[46%] rounded-xl px-3.5 py-2 text-sm max-md:max-w-[72%] ${
          owner
            ? 'rounded-br-[4px] bg-[#D8EFCB] text-[#1E3320]'
            : 'rounded-bl-[4px] border border-[#DDE4DD] bg-white text-[#1C2B25]'
        } ${preSignup ? 'border border-dashed border-[#B4560F]/50' : ''}`}
      >
        <div className="flex items-baseline gap-2">
          <span className="text-[11px] font-medium text-[#5C6B63]">{e.title}</span>
          <span className="font-mono text-[10px] text-[#93A399]">{String(e.ts).slice(11, 16)}Z</span>
          {preSignup ? (
            <span className="rounded-full bg-[#FBEFE3] px-2 py-px font-mono text-[9px] tracking-[0.05em] text-[#B4560F]">
              NOT LOGGED IN
            </span>
          ) : null}
        </div>
        <p className="mt-0.5 whitespace-pre-wrap [overflow-wrap:anywhere]">
          <Linkified text={e.body} />
        </p>
      </div>
    </div>
  )
}

function groupDesc(events: FlowEvent[]): Array<{ day: string; events: FlowEvent[] }> {
  const out: Array<{ day: string; events: FlowEvent[] }> = []
  for (const e of events) {
    const day = String(e.ts).slice(0, 10)
    const last = out[out.length - 1]
    if (last && last.day === day) last.events.push(e)
    else out.push({ day, events: [e] })
  }
  return out
}

export function FlowStream({
  tenantId,
  initialEvents,
  initialCursor,
}: {
  tenantId: string
  initialEvents: FlowEvent[]
  initialCursor: string | null
}) {
  const [events, setEvents] = useState<FlowEvent[]>(initialEvents)
  const [cursor, setCursor] = useState<string | null>(initialCursor)
  const [loading, setLoading] = useState(false)
  const sentinelRef = useRef<HTMLDivElement | null>(null)
  const busyRef = useRef(false)

  const loadMore = useCallback(async () => {
    if (busyRef.current || cursor === null) return
    busyRef.current = true
    setLoading(true)
    try {
      const page: FlowPage = await loadOlderFlow(tenantId, cursor)
      setEvents((prev) => [...prev, ...page.events])
      setCursor(page.nextBefore)
    } finally {
      busyRef.current = false
      setLoading(false)
    }
  }, [tenantId, cursor])

  useEffect(() => {
    const el = sentinelRef.current
    if (!el) return
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((en) => en.isIntersecting)) void loadMore()
      },
      { rootMargin: '600px' },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [loadMore])

  const days = groupDesc(events)

  return (
    <div data-flow-stream>
      {days.map(({ day, events: dayEvents }) => (
        <section
          key={day}
          id={`d-${day}`}
          className="relative mt-2 scroll-mt-16 space-y-4 pb-4 before:absolute before:top-0 before:bottom-0 before:left-1/2 before:w-[2px] before:-translate-x-1/2 before:bg-[#C9D4CC] before:content-[''] max-md:before:left-6"
          data-flow-day={day}
        >
          <h2 className="sticky top-2 z-20 mx-auto w-fit rounded-full bg-[#1C2B25] px-4 py-1 text-xs font-medium text-white">
            {day}
          </h2>
          {dayEvents.map((e, i) =>
            e.kind === 'message' ? (
              <MessageBubble key={`${day}-${i}`} e={e} />
            ) : (
              <SystemCard key={`${day}-${i}`} e={e} />
            ),
          )}
        </section>
      ))}
      <div ref={sentinelRef} className="h-8" />
      <p className="pb-6 text-center text-xs text-[#93A399]">
        {cursor === null ? '— start of history —' : loading ? 'loading older…' : 'scroll for older'}
      </p>
    </div>
  )
}
