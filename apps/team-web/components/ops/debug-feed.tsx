'use client'

/**
 * VT-515 — Ops Console "Debug / Failures" live feed.
 *
 * Summary-first, newest-on-top, capped at 200 rows in memory.
 * Combines a server-fetched initial batch with live Realtime INSERTs.
 *
 * Design contract:
 *   - Light-mode only (global lock).
 *   - Each row: HH:MM:SS · severity dot · component · summary line.
 *   - Prominent badge: total count + critical count.
 *   - Filters: component, severity, failure_type, free-text search, trace_id click.
 *   - Click row → inline drill-down panel (lazy, only when expanded).
 *   - PII boundary: error_message / error_stack are already redacted by orchestrator;
 *     render as-is, no further processing.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  browserStreamClient,
  subscribeDebugEvents,
  type DebugEvent,
  type DebugEventFilters,
} from '@/lib/ops/stream'
import { composeEventSummary } from '@/lib/ops/debug-events'
import { JsonPretty } from '@/components/ops/json-pretty'

const _MAX_ROWS = 200

// ─── Types ────────────────────────────────────────────────────────────────────

interface FeedFilters {
  component: string
  severity: string
  failure_type: string
  search: string
  trace_id: string
}

const _EMPTY_FILTERS: FeedFilters = {
  component: '',
  severity: '',
  failure_type: '',
  search: '',
  trace_id: '',
}

const _SEVERITY_OPTIONS = ['warning', 'error', 'critical'] as const
const _FAILURE_TYPE_OPTIONS = [
  'exception',
  'timeout',
  'vendor_error',
  'network',
  'validation',
  'crash',
  'silent_degrade',
] as const

// ─── Severity styling ──────────────────────────────────────────────────────────

function _severityDot(severity: DebugEvent['severity']): string {
  if (severity === 'critical') return 'inline-block h-2 w-2 rounded-full bg-red-600 ring-1 ring-red-300 shrink-0'
  if (severity === 'error') return 'inline-block h-2 w-2 rounded-full bg-red-400 shrink-0'
  return 'inline-block h-2 w-2 rounded-full bg-amber-400 shrink-0'
}

function _severityBadgeClass(severity: DebugEvent['severity']): string {
  if (severity === 'critical') return 'rounded-full px-2 py-0.5 text-xs font-bold bg-red-100 text-red-800 ring-1 ring-inset ring-red-300'
  if (severity === 'error') return 'rounded-full px-2 py-0.5 text-xs font-medium bg-red-50 text-red-700 ring-1 ring-inset ring-red-200'
  return 'rounded-full px-2 py-0.5 text-xs font-medium bg-amber-50 text-amber-800 ring-1 ring-inset ring-amber-200'
}

// ─── Row filtering (client-side, applied over the full event list) ─────────────

function _passesFilters(event: DebugEvent, f: FeedFilters): boolean {
  if (f.component && event.component !== f.component) return false
  if (f.severity && event.severity !== f.severity) return false
  if (f.failure_type && event.failure_type !== f.failure_type) return false
  if (f.trace_id && event.trace_id !== f.trace_id) return false
  if (f.search) {
    const q = f.search.toLowerCase()
    const haystack = [
      event.component,
      event.operation,
      event.failure_type,
      event.error_message,
      event.impact,
      event.trace_id,
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase()
    if (!haystack.includes(q)) return false
  }
  return true
}

// ─── Timestamp formatting ──────────────────────────────────────────────────────

function _fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString('en-IN', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    })
  } catch {
    return iso.slice(11, 19)
  }
}

// ─── Count badge ──────────────────────────────────────────────────────────────

function CountBadge({ total, critical }: { total: number; critical: number }) {
  return (
    <div
      data-element="debug-count-badge"
      className="flex items-center gap-3 rounded-lg border border-gray-200 bg-white px-4 py-3 shadow-sm"
    >
      <span className="text-sm font-medium text-gray-500">Events</span>
      <span
        data-element="total-count"
        className="text-2xl font-bold text-gray-900 tabular-nums"
      >
        {total}
      </span>
      {critical > 0 ? (
        <span
          data-element="critical-count"
          className="ml-1 flex items-center gap-1 rounded-full bg-red-600 px-3 py-0.5 text-sm font-bold text-white"
        >
          <span className="inline-block h-1.5 w-1.5 rounded-full bg-white" />
          {critical} critical
        </span>
      ) : (
        <span className="text-xs text-gray-400">no criticals</span>
      )}
    </div>
  )
}

// ─── Filter bar ───────────────────────────────────────────────────────────────

interface FilterBarProps {
  filters: FeedFilters
  onChange: (f: FeedFilters) => void
  onClear: () => void
}

function FilterBar({ filters, onChange, onClear }: FilterBarProps) {
  const hasActive =
    filters.component ||
    filters.severity ||
    filters.failure_type ||
    filters.search ||
    filters.trace_id

  return (
    <div
      data-element="debug-filter-bar"
      className="flex flex-wrap items-end gap-3 rounded-lg border border-gray-200 bg-gray-50 px-4 py-3"
    >
      {/* Free-text search */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500">Search</label>
        <input
          type="search"
          placeholder="component, trace, message…"
          value={filters.search}
          onChange={(e) => onChange({ ...filters, search: e.target.value })}
          className="h-8 w-52 rounded border border-gray-300 bg-white px-2.5 text-sm text-gray-900 placeholder-gray-400 focus:border-gray-500 focus:outline-none"
          data-filter="search"
        />
      </div>

      {/* Component */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500">Component</label>
        <input
          type="text"
          placeholder="e.g. discovery"
          value={filters.component}
          onChange={(e) => onChange({ ...filters, component: e.target.value.trim() })}
          className="h-8 w-36 rounded border border-gray-300 bg-white px-2.5 text-sm text-gray-900 placeholder-gray-400 focus:border-gray-500 focus:outline-none"
          data-filter="component"
        />
      </div>

      {/* Severity pills */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500">Severity</label>
        <div className="flex gap-1">
          {_SEVERITY_OPTIONS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => onChange({ ...filters, severity: filters.severity === s ? '' : s })}
              className={`h-8 rounded px-2.5 text-xs font-medium transition-colors ${
                filters.severity === s
                  ? s === 'critical'
                    ? 'bg-red-600 text-white'
                    : s === 'error'
                      ? 'bg-red-400 text-white'
                      : 'bg-amber-400 text-gray-900'
                  : 'border border-gray-300 bg-white text-gray-600 hover:bg-gray-50'
              }`}
              data-filter={`severity-${s}`}
              aria-pressed={filters.severity === s}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      {/* Failure type pills */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500">Failure type</label>
        <div className="flex flex-wrap gap-1">
          {_FAILURE_TYPE_OPTIONS.map((ft) => (
            <button
              key={ft}
              type="button"
              onClick={() => onChange({ ...filters, failure_type: filters.failure_type === ft ? '' : ft })}
              className={`h-8 rounded px-2.5 text-xs font-medium transition-colors ${
                filters.failure_type === ft
                  ? 'bg-gray-800 text-white'
                  : 'border border-gray-300 bg-white text-gray-600 hover:bg-gray-50'
              }`}
              data-filter={`failure-type-${ft}`}
              aria-pressed={filters.failure_type === ft}
            >
              {ft.replace(/_/g, ' ')}
            </button>
          ))}
        </div>
      </div>

      {/* Trace ID filter (set by clicking a trace_id in the list) */}
      {filters.trace_id ? (
        <div className="flex flex-col gap-1">
          <label className="text-xs font-medium text-gray-500">Trace</label>
          <div className="flex items-center gap-1.5 rounded border border-blue-300 bg-blue-50 px-2 py-1.5">
            <code className="text-xs text-blue-700">{filters.trace_id.slice(0, 16)}…</code>
            <button
              type="button"
              onClick={() => onChange({ ...filters, trace_id: '' })}
              className="text-blue-500 hover:text-blue-700"
              aria-label="Clear trace filter"
            >
              ×
            </button>
          </div>
        </div>
      ) : null}

      {/* Clear all */}
      {hasActive ? (
        <button
          type="button"
          onClick={onClear}
          className="h-8 self-end rounded border border-gray-300 bg-white px-3 text-xs text-gray-600 hover:bg-gray-50"
          data-element="clear-filters"
        >
          Clear all
        </button>
      ) : null}
    </div>
  )
}

// ─── Drill-down panel ─────────────────────────────────────────────────────────

function DrillDown({
  event,
  onFilterTrace,
}: {
  event: DebugEvent
  onFilterTrace: (traceId: string) => void
}) {
  return (
    <div
      data-element="debug-drill-down"
      className="border-t border-gray-100 bg-gray-50 px-4 py-3 text-xs text-gray-700"
    >
      <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-3">
        <Field label="failure_type" value={event.failure_type} />
        <Field label="component" value={event.component} />
        <Field label="operation" value={event.operation} />
        <Field label="severity" value={event.severity} />
        <Field label="impact" value={event.impact} />
        <Field label="vendor" value={event.vendor} />
        <Field label="vendor_status" value={event.vendor_status} />
        <Field
          label="latency_ms"
          value={event.latency_ms !== null ? `${event.latency_ms} ms` : null}
        />
        <div className="col-span-2 sm:col-span-1">
          <span className="font-medium text-gray-500">tenant_id</span>
          <div className="mt-0.5 font-mono text-gray-800">
            {event.tenant_id ?? <span className="italic text-gray-400">null (pre-tenant)</span>}
          </div>
        </div>
        {event.trace_id ? (
          <div className="col-span-2 sm:col-span-3">
            <span className="font-medium text-gray-500">trace_id</span>
            <div className="mt-0.5 flex items-center gap-2">
              <code className="font-mono text-gray-800">{event.trace_id}</code>
              <button
                type="button"
                onClick={() => onFilterTrace(event.trace_id!)}
                className="rounded bg-blue-50 px-1.5 py-0.5 text-xs text-blue-700 hover:bg-blue-100"
                data-element="filter-by-trace"
              >
                filter by trace
              </button>
            </div>
          </div>
        ) : null}
      </div>

      {event.error_message ? (
        <div className="mt-3">
          <div className="mb-1 font-medium text-gray-500">error_message (redacted)</div>
          <pre className="whitespace-pre-wrap rounded bg-rose-50 p-2 font-mono text-rose-800">
            {event.error_message}
          </pre>
        </div>
      ) : null}

      {event.error_stack ? (
        <div className="mt-3">
          <div className="mb-1 font-medium text-gray-500">error_stack (redacted)</div>
          <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap rounded bg-gray-100 p-2 font-mono text-gray-700">
            {event.error_stack}
          </pre>
        </div>
      ) : null}

      <JsonPretty label="context" value={event.context} />
    </div>
  )
}

function Field({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div>
      <span className="font-medium text-gray-500">{label}</span>
      <div className="mt-0.5 text-gray-800">
        {value ?? <span className="italic text-gray-400">—</span>}
      </div>
    </div>
  )
}

// ─── Feed row ─────────────────────────────────────────────────────────────────

interface FeedRowProps {
  event: DebugEvent
  expanded: boolean
  onToggle: () => void
  onFilterTrace: (traceId: string) => void
  isNew: boolean
}

function FeedRow({ event, expanded, onToggle, onFilterTrace, isNew }: FeedRowProps) {
  const summary = composeEventSummary(event)
  return (
    <li
      data-event-id={event.id}
      data-severity={event.severity}
      className={`border-b border-gray-100 transition-colors last:border-0 ${isNew ? 'animate-pulse-once bg-blue-50/60' : 'bg-white'}`}
    >
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2.5 px-4 py-2.5 text-left hover:bg-gray-50"
        aria-expanded={expanded}
      >
        {/* Timestamp */}
        <span className="w-20 shrink-0 font-mono text-xs text-gray-400 tabular-nums">
          {_fmtTime(event.created_at)}
        </span>
        {/* Severity dot */}
        <span className={_severityDot(event.severity)} aria-label={event.severity} />
        {/* Component chip */}
        <span className="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 font-mono text-xs text-gray-700">
          {event.component}
        </span>
        {/* Summary */}
        <span className="min-w-0 flex-1 truncate text-sm text-gray-800">{summary}</span>
        {/* Expand indicator */}
        <span className="shrink-0 text-xs text-gray-400">{expanded ? '▲' : '▼'}</span>
      </button>
      {expanded ? (
        <DrillDown event={event} onFilterTrace={onFilterTrace} />
      ) : null}
    </li>
  )
}

// ─── Main component ────────────────────────────────────────────────────────────

export interface DebugFeedProps {
  /** Short-lived operator JWT for Supabase Realtime (VTAdmin only path). */
  operatorJwt: string
  /** Server-prefetched initial batch (newest first). */
  initialEvents: DebugEvent[]
}

export function DebugFeed({ operatorJwt, initialEvents }: DebugFeedProps) {
  // All events (initial + live), newest first, capped at _MAX_ROWS.
  const [events, setEvents] = useState<DebugEvent[]>(() => initialEvents.slice(0, _MAX_ROWS))
  const [filters, setFilters] = useState<FeedFilters>(_EMPTY_FILTERS)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [connected, setConnected] = useState(false)
  // Track IDs of newly arrived events for the flash animation.
  const [newIds, setNewIds] = useState<Set<string>>(new Set())

  const onEvent = useCallback((event: DebugEvent) => {
    setEvents((prev) => [event, ...prev].slice(0, _MAX_ROWS))
    setNewIds((prev) => {
      const next = new Set(prev)
      next.add(event.id)
      return next
    })
    // Remove the "new" highlight after 3 s.
    setTimeout(() => {
      setNewIds((prev) => {
        const next = new Set(prev)
        next.delete(event.id)
        return next
      })
    }, 3000)
  }, [])

  // Build Realtime filters from the active UI filters (only fields the subscription supports).
  const realtimeFilters: DebugEventFilters = useMemo(() => {
    const f: DebugEventFilters = {}
    if (filters.component) f.component = filters.component
    if (filters.severity) f.severity = filters.severity
    return f
  }, [filters.component, filters.severity])

  useEffect(() => {
    let unsubscribe: (() => void) | null = null
    let cancelled = false
    queueMicrotask(() => {
      if (cancelled) return
      try {
        const client = browserStreamClient(operatorJwt)
        unsubscribe = subscribeDebugEvents(client, realtimeFilters, onEvent)
        setConnected(true)
      } catch (err) {
        console.error('VT-515 debug-events subscribe failed', err)
        setConnected(false)
      }
    })
    return () => {
      cancelled = true
      unsubscribe?.()
      setConnected(false)
    }
  }, [operatorJwt, realtimeFilters, onEvent])

  // Client-side filtering for failure_type + search + trace_id (not in Realtime filter).
  const visible = useMemo(
    () => events.filter((e) => _passesFilters(e, filters)),
    [events, filters],
  )

  const totalCount = visible.length
  const criticalCount = useMemo(
    () => visible.filter((e) => e.severity === 'critical').length,
    [visible],
  )

  const onFilterTrace = useCallback((traceId: string) => {
    setFilters((f) => ({ ...f, trace_id: traceId }))
  }, [])

  const onToggleRow = useCallback((id: string) => {
    setExpandedId((prev) => (prev === id ? null : id))
  }, [])

  return (
    <div data-component="debug-feed" className="space-y-4">
      {/* Header: badge + connection status */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <CountBadge total={totalCount} critical={criticalCount} />
        <div
          data-element="realtime-status"
          className={`flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset ${
            connected
              ? 'bg-emerald-50 text-emerald-700 ring-emerald-600/20'
              : 'bg-gray-100 text-gray-500 ring-gray-400/20'
          }`}
        >
          <span
            className={`inline-block h-1.5 w-1.5 rounded-full ${connected ? 'bg-emerald-500' : 'bg-gray-400'}`}
          />
          {connected ? 'live' : 'disconnected'}
        </div>
      </div>

      {/* Filter bar */}
      <FilterBar
        filters={filters}
        onChange={setFilters}
        onClear={() => setFilters(_EMPTY_FILTERS)}
      />

      {/* Event list */}
      <div
        data-element="debug-event-list"
        className="overflow-hidden rounded-lg border border-gray-200 bg-white shadow-sm"
      >
        {visible.length === 0 ? (
          <div
            data-element="debug-empty"
            className="px-6 py-12 text-center text-sm text-gray-500"
          >
            {events.length === 0 ? 'No debug events yet.' : 'No events match the current filters.'}
          </div>
        ) : (
          <ul role="list">
            {visible.map((event) => (
              <FeedRow
                key={event.id}
                event={event}
                expanded={expandedId === event.id}
                onToggle={() => onToggleRow(event.id)}
                onFilterTrace={onFilterTrace}
                isNew={newIds.has(event.id)}
              />
            ))}
          </ul>
        )}
      </div>

      {events.length >= _MAX_ROWS ? (
        <p className="text-center text-xs text-gray-400">
          Showing the {_MAX_ROWS} most recent events. Older events are not shown.
        </p>
      ) : null}
    </div>
  )
}
