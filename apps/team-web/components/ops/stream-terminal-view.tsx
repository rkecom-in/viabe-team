/**
 * VT-238 — terminal-style live stream view.
 *
 * Drop-in replacement for StreamRowList inside StreamFeed. Renders rows
 * as terminal-aesthetic single-line entries with:
 * - HH:MM:SS.mmm prefix
 * - color-per-column (cyan tenant, amber step, blue run, green/red/yellow status)
 * - click-to-expand inline JsonPretty for input/output envelopes
 * - free-text search input
 * - auto-tail with pause-on-scroll-up + "↓ Resume tailing" button
 *
 * History view (`StreamHistoryView`) still uses StreamRowList — terminal
 * redesign is live-only per brief LOCK 2.
 */

'use client'

import { useEffect, useMemo, useRef, useState } from 'react'

import { JsonPretty } from '@/components/ops/json-pretty'
import type { PipelineStepEvent } from '@/lib/ops/stream'

export interface StreamTerminalViewProps {
  rows: PipelineStepEvent[]
  tenantName: (tenantId: string) => string
}

function fmtTime(iso: string): string {
  const d = new Date(iso)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  const ms = String(d.getMilliseconds()).padStart(3, '0')
  return `${hh}:${mm}:${ss}.${ms}`
}

function statusColor(status: string): string {
  switch (status) {
    case 'completed':
    case 'ok':
    case 'success':
      return 'text-green-400'
    case 'failed':
    case 'error':
      return 'text-red-400'
    case 'running':
      return 'text-blue-400'
    case 'skipped':
      return 'text-yellow-400'
    default:
      return 'text-gray-400'
  }
}

function shortId(id: string, n = 8): string {
  return id.slice(0, n)
}

function snippetFor(step: PipelineStepEvent): string {
  if (step.step_name) return step.step_name
  if (step.model_used) return step.model_used
  return ''
}

interface TerminalRowProps {
  step: PipelineStepEvent
  tenantLabel: string
  expanded: boolean
  onToggle: () => void
}

function TerminalRow({ step, tenantLabel, expanded, onToggle }: TerminalRowProps) {
  return (
    <li
      data-step-id={step.id}
      data-step-kind={step.step_kind}
      data-step-status={step.status}
      data-element="terminal-row"
      className="border-b border-gray-800 hover:bg-gray-800"
    >
      <button
        type="button"
        onClick={onToggle}
        className="w-full text-left px-2 py-0.5 flex items-baseline gap-3 cursor-pointer"
        data-element="terminal-row-trigger"
      >
        <span data-col="started_at" className="text-gray-500">
          {fmtTime(step.started_at)}
        </span>
        <span data-col="tenant_name" className="text-cyan-400 truncate max-w-[14ch]">
          {tenantLabel}
        </span>
        <a
          data-col="run_id"
          href={`/team/ops/runs/${step.run_id}`}
          onClick={(e) => e.stopPropagation()}
          className="text-blue-400 hover:underline"
        >
          {shortId(step.run_id)}
        </a>
        <span data-col="step_kind" className="text-amber-400">
          {step.step_kind}
        </span>
        <span data-col="status" className={statusColor(step.status)}>
          {step.status}
        </span>
        <span data-col="snippet" className="text-gray-300 truncate flex-1">
          {snippetFor(step)}
        </span>
        <span data-col="duration_ms" className="text-gray-500">
          {step.duration_ms ?? '—'}ms
        </span>
        <span data-col="cost_paise" className="text-gray-500">
          {step.cost_paise ?? 0}p
        </span>
      </button>
      {expanded ? (
        <div data-element="terminal-row-detail" className="px-4 py-2 bg-gray-950">
          <JsonPretty
            label="input_envelope"
            value={step.input_envelope}
            defaultOpen={true}
          />
          <JsonPretty
            label="output_envelope"
            value={step.output_envelope}
            defaultOpen={true}
          />
        </div>
      ) : null}
    </li>
  )
}

export function StreamTerminalView({
  rows,
  tenantName,
}: StreamTerminalViewProps) {
  const [search, setSearch] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [tailing, setTailing] = useState(true)
  const containerRef = useRef<HTMLDivElement | null>(null)

  const filtered = useMemo(() => {
    if (!search.trim()) return rows
    const q = search.trim().toLowerCase()
    return rows.filter((step) => {
      return (
        step.step_kind.toLowerCase().includes(q)
        || (step.step_name ?? '').toLowerCase().includes(q)
        || step.status.toLowerCase().includes(q)
        || step.tenant_id.toLowerCase().includes(q)
        || step.run_id.toLowerCase().includes(q)
        || tenantName(step.tenant_id).toLowerCase().includes(q)
      )
    })
  }, [rows, search, tenantName])

  useEffect(() => {
    if (!tailing) return
    const el = containerRef.current
    if (!el) return
    el.scrollTop = 0  // rows are newest-first, so "tail" = top
  }, [tailing, filtered.length])

  const onScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget
    // Newest-first list: at-top means tailing; pause when user scrolls
    // away from the top by >24px.
    if (el.scrollTop > 24 && tailing) setTailing(false)
    if (el.scrollTop <= 24 && !tailing) setTailing(true)
  }

  return (
    <div
      data-component="stream-terminal-view"
      className="bg-gray-900 text-gray-100 font-mono text-xs rounded border border-gray-800"
    >
      <div
        data-section="terminal-filter-bar"
        className="sticky top-0 z-10 bg-gray-900 border-b border-gray-800 p-2 flex gap-2 items-center"
      >
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="search…"
          data-element="terminal-search"
          className="bg-gray-800 text-gray-100 placeholder-gray-500 px-2 py-1 rounded border border-gray-700 focus:outline-none focus:border-gray-500 flex-1"
        />
        {!tailing ? (
          <button
            type="button"
            onClick={() => setTailing(true)}
            data-element="resume-tailing"
            className="bg-amber-900 text-amber-100 px-2 py-1 rounded hover:bg-amber-800"
          >
            ↓ Resume tailing
          </button>
        ) : (
          <span
            data-element="tailing-indicator"
            className="text-green-400 px-2"
          >
            ● tailing
          </span>
        )}
      </div>
      <div
        ref={containerRef}
        onScroll={onScroll}
        data-section="terminal-rows-scroller"
        className="max-h-[70vh] overflow-y-auto"
      >
        {filtered.length === 0 ? (
          <p
            data-element="empty-state"
            className="text-gray-500 italic px-4 py-3"
          >
            waiting for events…
          </p>
        ) : (
          <ol data-section="terminal-rows">
            {filtered.map((step) => (
              <TerminalRow
                key={step.id}
                step={step}
                tenantLabel={tenantName(step.tenant_id)}
                expanded={expandedId === step.id}
                onToggle={() =>
                  setExpandedId(expandedId === step.id ? null : step.id)
                }
              />
            ))}
          </ol>
        )}
      </div>
    </div>
  )
}
