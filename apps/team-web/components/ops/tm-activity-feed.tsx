'use client'

/**
 * VT-516 — Ops Console "Team Manager activity" live feed (per-tenant page).
 *
 * Streams tm_audit_log (KNOWS / GETS / DECIDES / DOES / ASKS) and, via a toggle,
 * merges the correlated debug_events failure stream into ONE timeline by trace_id.
 *
 * Design contract:
 *   - Light-mode only (hardcoded Tailwind grays; NO dark: variants, NO CSS tokens).
 *   - Summary-first, GROUPED by run_id (fallback trace_id) into collapsible group
 *     headers, COLLAPSED BY DEFAULT. The grouping is the one net-new bit vs the flat
 *     VT-515 debug feed; everything else mirrors components/ops/debug-feed.tsx.
 *   - Each event row: HH:MM:SS · severity dot · layer/source chip · actor · summary.
 *   - Click a row → lazy inline drill-down (snapshot_id, decision, action, result,
 *     reasoning_ref via JsonPretty; failures show severity/status).
 *   - Controls: event_layer / event_kind / severity / FAILURES toggle / free-text
 *     search / audit·debug·both view toggle + a failures count badge.
 *   - tenant_id is ALWAYS pinned to the page's tenantId so the Realtime feed is
 *     never unscoped (no cross-tenant leak).
 *   - PII boundary: every column is redacted by the orchestrator at emit time
 *     (CL-390). Render ids + structured facts as-is; no further processing.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  browserStreamClient,
  subscribeDebugEvents,
  subscribeTmAuditEvents,
  type DebugEvent,
  type DebugEventFilters,
  type TmAuditEvent,
  type TmAuditFilters,
} from '@/lib/ops/stream'
import { composeTmAuditSummary } from '@/lib/ops/tm-audit-events'
import { composeEventSummary } from '@/lib/ops/debug-events'
import { JsonPretty } from '@/components/ops/json-pretty'

const _MAX_ROWS = 200

// ─── Filters / view ─────────────────────────────────────────────────────────────

type FeedView = 'both' | 'audit' | 'debug'

interface AuditFeedFilters {
  event_layer: string
  event_kind: string
  severity: string
  search: string
  failures: boolean
}

const _EMPTY_FILTERS: AuditFeedFilters = {
  event_layer: '',
  event_kind: '',
  severity: '',
  search: '',
  failures: false,
}

const _LAYER_OPTIONS = ['knows', 'gets', 'decides', 'does', 'asks'] as const
const _SEVERITY_OPTIONS = ['info', 'warning', 'error', 'critical'] as const

// ─── Unified row (audit ∪ debug, merged by trace_id) ──────────────────────────────

interface UnifiedRowBase {
  key: string // `${source}:${id}` — unique React key across both streams
  source: 'audit' | 'debug'
  id: string
  ts: string // created_at (ISO)
  severity: string
  run_id: string | null
  trace_id: string | null
  summary: string
  haystack: string
  isFailure: boolean
}
interface UnifiedAuditRow extends UnifiedRowBase {
  source: 'audit'
  audit: TmAuditEvent
}
interface UnifiedDebugRow extends UnifiedRowBase {
  source: 'debug'
  debug: DebugEvent
}
type UnifiedRow = UnifiedAuditRow | UnifiedDebugRow

function _isFailureSeverity(severity: string): boolean {
  return severity === 'error' || severity === 'critical'
}

function toUnifiedAudit(e: TmAuditEvent): UnifiedAuditRow {
  return {
    key: `audit:${e.id}`,
    source: 'audit',
    id: e.id,
    ts: e.created_at,
    severity: e.severity,
    run_id: e.run_id,
    trace_id: e.trace_id,
    summary: composeTmAuditSummary(e),
    haystack: [e.actor, e.event_layer, e.event_kind, e.summary, e.trace_id, e.run_id, e.status]
      .filter(Boolean)
      .join(' ')
      .toLowerCase(),
    isFailure: e.status === 'failed' || _isFailureSeverity(e.severity),
    audit: e,
  }
}

function toUnifiedDebug(e: DebugEvent): UnifiedDebugRow {
  return {
    key: `debug:${e.id}`,
    source: 'debug',
    id: e.id,
    ts: e.created_at,
    severity: e.severity,
    run_id: null,
    trace_id: e.trace_id,
    summary: composeEventSummary(e),
    haystack: [e.component, e.operation, e.failure_type, e.error_message, e.impact, e.trace_id]
      .filter(Boolean)
      .join(' ')
      .toLowerCase(),
    isFailure: _isFailureSeverity(e.severity),
    debug: e,
  }
}

/** Group key: run_id, else trace_id, else 'ungrouped'. Because an audit row's
 *  trace_id == str(run_id) and a debug row's trace_id is the SAME correlation
 *  value, audit + debug rows of one run land in the same group. */
function groupKeyOf(row: UnifiedRow): string {
  return row.run_id ?? row.trace_id ?? 'ungrouped'
}

function groupLabel(row: UnifiedRow): string {
  if (row.run_id) return `run ${row.run_id.slice(0, 8)}`
  if (row.trace_id) return `trace ${row.trace_id.slice(0, 16)}`
  return 'ungrouped'
}

/** Display predicate over a unified row (event_layer/event_kind are audit-only —
 *  an active value hides debug rows, which cannot satisfy them). Exported for
 *  parity with the debug feed's _passesFilters. */
export function _passesUnifiedFilters(row: UnifiedRow, f: AuditFeedFilters): boolean {
  if (f.event_layer) {
    if (row.source !== 'audit' || row.audit.event_layer !== f.event_layer) return false
  }
  if (f.event_kind) {
    if (row.source !== 'audit' || row.audit.event_kind !== f.event_kind) return false
  }
  if (f.severity && row.severity !== f.severity) return false
  if (f.failures && !row.isFailure) return false
  if (f.search && !row.haystack.includes(f.search.toLowerCase())) return false
  return true
}

// ─── Severity styling ──────────────────────────────────────────────────────────

function _severityDot(severity: string): string {
  if (severity === 'critical') return 'inline-block h-2 w-2 rounded-full bg-red-600 ring-1 ring-red-300 shrink-0'
  if (severity === 'error') return 'inline-block h-2 w-2 rounded-full bg-red-400 shrink-0'
  if (severity === 'warning') return 'inline-block h-2 w-2 rounded-full bg-amber-400 shrink-0'
  return 'inline-block h-2 w-2 rounded-full bg-gray-400 shrink-0'
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

function AuditCountBadge({ total, failures }: { total: number; failures: number }) {
  return (
    <div
      data-element="tm-audit-count-badge"
      className="flex items-center gap-3 rounded-lg border border-gray-200 bg-white px-4 py-3 shadow-sm"
    >
      <span className="text-sm font-medium text-gray-500">Events</span>
      <span data-element="total-count" className="text-2xl font-bold text-gray-900 tabular-nums">
        {total}
      </span>
      {failures > 0 ? (
        <span
          data-element="failures-count"
          className="ml-1 flex items-center gap-1 rounded-full bg-red-600 px-3 py-0.5 text-sm font-bold text-white"
        >
          <span className="inline-block h-1.5 w-1.5 rounded-full bg-white" />
          {failures} failed
        </span>
      ) : (
        <span className="text-xs text-gray-400">no failures</span>
      )}
    </div>
  )
}

// ─── View toggle (audit · debug · both) ─────────────────────────────────────────

function ViewToggle({ view, onChange }: { view: FeedView; onChange: (v: FeedView) => void }) {
  const opts: { id: FeedView; label: string }[] = [
    { id: 'both', label: 'both' },
    { id: 'audit', label: 'audit' },
    { id: 'debug', label: 'debug' },
  ]
  return (
    <div data-element="view-toggle" className="inline-flex rounded-md border border-gray-300 bg-white p-0.5">
      {opts.map((o) => (
        <button
          key={o.id}
          type="button"
          onClick={() => onChange(o.id)}
          className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
            view === o.id ? 'bg-gray-900 text-white' : 'text-gray-600 hover:bg-gray-100'
          }`}
          data-view={o.id}
          aria-pressed={view === o.id}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

// ─── Filter bar ───────────────────────────────────────────────────────────────

interface FilterBarProps {
  filters: AuditFeedFilters
  onChange: (f: AuditFeedFilters) => void
  onClear: () => void
}

function AuditFilterBar({ filters, onChange, onClear }: FilterBarProps) {
  const hasActive =
    filters.event_layer || filters.event_kind || filters.severity || filters.search || filters.failures

  return (
    <div
      data-element="tm-audit-filter-bar"
      className="flex flex-wrap items-end gap-3 rounded-lg border border-gray-200 bg-gray-50 px-4 py-3"
    >
      {/* Free-text search */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500">Search</label>
        <input
          type="search"
          placeholder="actor, kind, trace, summary…"
          value={filters.search}
          onChange={(e) => onChange({ ...filters, search: e.target.value })}
          className="h-8 w-56 rounded border border-gray-300 bg-white px-2.5 text-sm text-gray-900 placeholder-gray-400 focus:border-gray-500 focus:outline-none"
          data-filter="search"
        />
      </div>

      {/* Event layer pills */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500">Layer</label>
        <div className="flex gap-1">
          {_LAYER_OPTIONS.map((l) => (
            <button
              key={l}
              type="button"
              onClick={() => onChange({ ...filters, event_layer: filters.event_layer === l ? '' : l })}
              className={`h-8 rounded px-2.5 text-xs font-medium transition-colors ${
                filters.event_layer === l
                  ? 'bg-gray-800 text-white'
                  : 'border border-gray-300 bg-white text-gray-600 hover:bg-gray-50'
              }`}
              data-filter={`layer-${l}`}
              aria-pressed={filters.event_layer === l}
            >
              {l}
            </button>
          ))}
        </div>
      </div>

      {/* Event kind (free text — too many kinds to pill) */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500">Kind</label>
        <input
          type="text"
          placeholder="e.g. route_decided"
          value={filters.event_kind}
          onChange={(e) => onChange({ ...filters, event_kind: e.target.value.trim() })}
          className="h-8 w-40 rounded border border-gray-300 bg-white px-2.5 text-sm text-gray-900 placeholder-gray-400 focus:border-gray-500 focus:outline-none"
          data-filter="event_kind"
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
                      : s === 'warning'
                        ? 'bg-amber-400 text-gray-900'
                        : 'bg-gray-700 text-white'
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

      {/* Failures-only toggle */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-gray-500">Failures</label>
        <button
          type="button"
          onClick={() => onChange({ ...filters, failures: !filters.failures })}
          className={`h-8 rounded px-3 text-xs font-medium transition-colors ${
            filters.failures
              ? 'bg-red-600 text-white'
              : 'border border-gray-300 bg-white text-gray-600 hover:bg-gray-50'
          }`}
          data-filter="failures-only"
          aria-pressed={filters.failures}
        >
          failures only
        </button>
      </div>

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

// ─── Field helper ───────────────────────────────────────────────────────────────

function Field({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div>
      <span className="font-medium text-gray-500">{label}</span>
      <div className="mt-0.5 break-words text-gray-800">
        {value ?? <span className="italic text-gray-400">—</span>}
      </div>
    </div>
  )
}

// ─── Drill-down panels ─────────────────────────────────────────────────────────

function AuditDrillDown({ event }: { event: TmAuditEvent }) {
  return (
    <div
      data-element="tm-audit-drill-down"
      className="border-t border-gray-100 bg-gray-50 px-4 py-3 text-xs text-gray-700"
    >
      <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-3">
        <Field label="event_layer" value={event.event_layer} />
        <Field label="event_kind" value={event.event_kind} />
        <Field label="actor" value={event.actor} />
        <Field label="severity" value={event.severity} />
        <Field label="status" value={event.status} />
        <Field label="snapshot_id" value={event.snapshot_id} />
        <Field label="run_id" value={event.run_id} />
        <Field label="parent_audit_id" value={event.parent_audit_id} />
        <div className="col-span-2 sm:col-span-1">
          <span className="font-medium text-gray-500">tenant_id</span>
          <div className="mt-0.5 font-mono text-gray-800">{event.tenant_id}</div>
        </div>
        {event.trace_id ? (
          <div className="col-span-2 sm:col-span-3">
            <span className="font-medium text-gray-500">trace_id</span>
            <div className="mt-0.5 font-mono text-gray-800">{event.trace_id}</div>
          </div>
        ) : null}
      </div>

      <JsonPretty label="input" value={event.input} />
      <JsonPretty label="decision" value={event.decision} />
      <JsonPretty label="action" value={event.action} />
      <JsonPretty label="result" value={event.result} />
      <JsonPretty label="reasoning_ref" value={event.reasoning_ref} />
    </div>
  )
}

function DebugDrillDown({ event }: { event: DebugEvent }) {
  return (
    <div
      data-element="tm-debug-drill-down"
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
        <Field label="latency_ms" value={event.latency_ms !== null ? `${event.latency_ms} ms` : null} />
        {event.trace_id ? (
          <div className="col-span-2 sm:col-span-3">
            <span className="font-medium text-gray-500">trace_id</span>
            <div className="mt-0.5 font-mono text-gray-800">{event.trace_id}</div>
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

      <JsonPretty label="context" value={event.context} />
    </div>
  )
}

// ─── Event row ───────────────────────────────────────────────────────────────

interface EventRowProps {
  row: UnifiedRow
  expanded: boolean
  onToggle: () => void
  isNew: boolean
}

function EventRow({ row, expanded, onToggle, isNew }: EventRowProps) {
  const chipLabel = row.source === 'audit' ? row.audit.event_layer : 'debug'
  const actorLabel = row.source === 'audit' ? row.audit.actor : row.debug.component
  return (
    <li
      data-event-id={row.id}
      data-source={row.source}
      data-severity={row.severity}
      className={`border-b border-gray-100 transition-colors last:border-0 ${
        isNew ? 'animate-pulse-once bg-blue-50/60' : 'bg-white'
      }`}
    >
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2.5 px-4 py-2 pl-8 text-left hover:bg-gray-50"
        aria-expanded={expanded}
      >
        <span className="w-20 shrink-0 font-mono text-xs text-gray-400 tabular-nums">{_fmtTime(row.ts)}</span>
        <span className={_severityDot(row.severity)} aria-label={row.severity} />
        <span
          className={`shrink-0 rounded px-1.5 py-0.5 font-mono text-xs ${
            row.source === 'debug' ? 'bg-rose-100 text-rose-700' : 'bg-gray-100 text-gray-700'
          }`}
        >
          {chipLabel}
        </span>
        <span className="shrink-0 truncate text-xs font-medium text-gray-500">{actorLabel}</span>
        <span className="min-w-0 flex-1 truncate text-sm text-gray-800">{row.summary}</span>
        <span className="shrink-0 text-xs text-gray-400">{expanded ? '▲' : '▼'}</span>
      </button>
      {expanded ? (
        row.source === 'audit' ? (
          <AuditDrillDown event={row.audit} />
        ) : (
          <DebugDrillDown event={row.debug} />
        )
      ) : null}
    </li>
  )
}

// ─── Group header + body ─────────────────────────────────────────────────────

interface FeedGroup {
  key: string
  label: string
  rows: UnifiedRow[]
  count: number
  failures: number
  latestTs: string
}

function GroupBlock({
  group,
  open,
  onToggleGroup,
  expandedKey,
  onToggleRow,
  newKeys,
}: {
  group: FeedGroup
  open: boolean
  onToggleGroup: () => void
  expandedKey: string | null
  onToggleRow: (key: string) => void
  newKeys: Set<string>
}) {
  return (
    <div data-element="tm-audit-group" data-group-key={group.key}>
      <button
        type="button"
        onClick={onToggleGroup}
        className="flex w-full items-center gap-2.5 border-b border-gray-100 bg-gray-50 px-4 py-2.5 text-left hover:bg-gray-100"
        aria-expanded={open}
        data-element="group-header"
      >
        <span className="shrink-0 text-xs text-gray-400">{open ? '▾' : '▸'}</span>
        <span className="shrink-0 font-mono text-xs font-medium text-gray-700">{group.label}</span>
        <span className="shrink-0 rounded-full bg-gray-200 px-2 py-0.5 text-xs font-medium text-gray-700 tabular-nums">
          {group.count} event{group.count === 1 ? '' : 's'}
        </span>
        {group.failures > 0 ? (
          <span className="shrink-0 rounded-full bg-red-600 px-2 py-0.5 text-xs font-bold text-white tabular-nums">
            {group.failures} failed
          </span>
        ) : null}
        <span className="ml-auto shrink-0 font-mono text-xs text-gray-400 tabular-nums">
          {_fmtTime(group.latestTs)}
        </span>
      </button>
      {open ? (
        <ul role="list">
          {group.rows.map((row) => (
            <EventRow
              key={row.key}
              row={row}
              expanded={expandedKey === row.key}
              onToggle={() => onToggleRow(row.key)}
              isNew={newKeys.has(row.key)}
            />
          ))}
        </ul>
      ) : null}
    </div>
  )
}

// ─── Main component ────────────────────────────────────────────────────────────

export interface TmActivityFeedProps {
  /** Pre-scoped; already access-gated server-side (canAccessTenant). */
  tenantId: string
  /** Short-lived operator JWT for Supabase Realtime (from issueOperatorJwt). */
  operatorJwt: string
  /** Optional server-prefetched audit batch (newest first). */
  initialEvents?: TmAuditEvent[]
  /** Optional server-prefetched debug batch (newest first). */
  initialDebugEvents?: DebugEvent[]
}

export function TmActivityFeed({
  tenantId,
  operatorJwt,
  initialEvents = [],
  initialDebugEvents = [],
}: TmActivityFeedProps) {
  const [tmEvents, setTmEvents] = useState<TmAuditEvent[]>(() => initialEvents.slice(0, _MAX_ROWS))
  const [debugEvents, setDebugEvents] = useState<DebugEvent[]>(() => initialDebugEvents.slice(0, _MAX_ROWS))
  const [filters, setFilters] = useState<AuditFeedFilters>(_EMPTY_FILTERS)
  const [view, setView] = useState<FeedView>('both')
  const [expandedKey, setExpandedKey] = useState<string | null>(null)
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set()) // collapsed by default
  const [tmConnected, setTmConnected] = useState(false)
  const [debugConnected, setDebugConnected] = useState(false)
  const [newKeys, setNewKeys] = useState<Set<string>>(new Set())

  const flashNew = useCallback((key: string) => {
    setNewKeys((prev) => {
      const next = new Set(prev)
      next.add(key)
      return next
    })
    setTimeout(() => {
      setNewKeys((prev) => {
        const next = new Set(prev)
        next.delete(key)
        return next
      })
    }, 3000)
  }, [])

  const onTmEvent = useCallback(
    (event: TmAuditEvent) => {
      setTmEvents((prev) => [event, ...prev].slice(0, _MAX_ROWS))
      flashNew(`audit:${event.id}`)
    },
    [flashNew],
  )

  const onDebugEvent = useCallback(
    (event: DebugEvent) => {
      setDebugEvents((prev) => [event, ...prev].slice(0, _MAX_ROWS))
      flashNew(`debug:${event.id}`)
    },
    [flashNew],
  )

  // tenant_id is ALWAYS pinned → the subscription is scoped; never unfiltered.
  // All other filters are applied client-side over the in-memory list so toggling
  // a filter never re-subscribes (and never drops already-received events).
  const realtimeFilters: TmAuditFilters = useMemo(() => ({ tenant_id: tenantId }), [tenantId])
  const debugRealtimeFilters: DebugEventFilters = useMemo(() => ({ tenant_id: tenantId }), [tenantId])

  useEffect(() => {
    let unsubTm: (() => void) | null = null
    let unsubDebug: (() => void) | null = null
    let cancelled = false
    queueMicrotask(() => {
      if (cancelled) return
      try {
        const client = browserStreamClient(operatorJwt)
        unsubTm = subscribeTmAuditEvents(client, realtimeFilters, onTmEvent)
        setTmConnected(true)
        unsubDebug = subscribeDebugEvents(client, debugRealtimeFilters, onDebugEvent)
        setDebugConnected(true)
      } catch (err) {
        console.error('VT-516 tm-audit subscribe failed', err)
        setTmConnected(false)
        setDebugConnected(false)
      }
    })
    return () => {
      cancelled = true
      unsubTm?.()
      unsubDebug?.()
      setTmConnected(false)
      setDebugConnected(false)
    }
  }, [operatorJwt, realtimeFilters, debugRealtimeFilters, onTmEvent, onDebugEvent])

  // Merge both streams into one timeline, view-filtered + display-filtered, newest first.
  const unifiedRows = useMemo(() => {
    const rows: UnifiedRow[] = []
    if (view !== 'debug') {
      for (const e of tmEvents) {
        const r = toUnifiedAudit(e)
        if (_passesUnifiedFilters(r, filters)) rows.push(r)
      }
    }
    if (view !== 'audit') {
      for (const e of debugEvents) {
        const r = toUnifiedDebug(e)
        if (_passesUnifiedFilters(r, filters)) rows.push(r)
      }
    }
    rows.sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0))
    return rows
  }, [tmEvents, debugEvents, view, filters])

  // Group by run_id/trace_id, preserving newest-first order (Map keeps insertion order).
  const groups = useMemo<FeedGroup[]>(() => {
    const m = new Map<string, UnifiedRow[]>()
    for (const r of unifiedRows) {
      const k = groupKeyOf(r)
      const arr = m.get(k)
      if (arr) arr.push(r)
      else m.set(k, [r])
    }
    return [...m.entries()].map(([key, rows]) => {
      // rows is non-empty by construction (an entry is created only on first push).
      const first = rows[0]!
      return {
        key,
        label: groupLabel(first),
        rows,
        count: rows.length,
        failures: rows.filter((r) => r.isFailure).length,
        latestTs: first.ts,
      }
    })
  }, [unifiedRows])

  const totalCount = unifiedRows.length
  const failuresCount = useMemo(() => unifiedRows.filter((r) => r.isFailure).length, [unifiedRows])
  const connected = tmConnected || debugConnected
  const rawCount = tmEvents.length + debugEvents.length

  const onToggleRow = useCallback((key: string) => {
    setExpandedKey((prev) => (prev === key ? null : key))
  }, [])

  const onToggleGroup = useCallback((key: string) => {
    setOpenGroups((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const expandAll = useCallback(() => {
    setOpenGroups(new Set(groups.map((g) => g.key)))
  }, [groups])

  const collapseAll = useCallback(() => {
    setOpenGroups(new Set())
  }, [])

  return (
    <div data-component="tm-activity-feed" data-tenant-id={tenantId} className="space-y-4">
      {/* Header: badge + view toggle + connection status */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <AuditCountBadge total={totalCount} failures={failuresCount} />
        <div className="flex items-center gap-3">
          <ViewToggle view={view} onChange={setView} />
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
      </div>

      {/* Filter bar */}
      <AuditFilterBar filters={filters} onChange={setFilters} onClear={() => setFilters(_EMPTY_FILTERS)} />

      {/* Group expand controls */}
      {groups.length > 0 ? (
        <div className="flex items-center gap-2 text-xs">
          <span className="text-gray-500">
            {groups.length} group{groups.length === 1 ? '' : 's'}
          </span>
          <button
            type="button"
            onClick={expandAll}
            className="rounded border border-gray-300 bg-white px-2 py-0.5 text-gray-600 hover:bg-gray-50"
            data-element="expand-all"
          >
            expand all
          </button>
          <button
            type="button"
            onClick={collapseAll}
            className="rounded border border-gray-300 bg-white px-2 py-0.5 text-gray-600 hover:bg-gray-50"
            data-element="collapse-all"
          >
            collapse all
          </button>
        </div>
      ) : null}

      {/* Grouped timeline */}
      <div
        data-element="tm-audit-event-list"
        className="overflow-hidden rounded-lg border border-gray-200 bg-white shadow-sm"
      >
        {groups.length === 0 ? (
          <div data-element="tm-audit-empty" className="px-6 py-12 text-center text-sm text-gray-500">
            {rawCount === 0 ? 'No Team Manager activity yet.' : 'No events match the current filters.'}
          </div>
        ) : (
          groups.map((group) => (
            <GroupBlock
              key={group.key}
              group={group}
              open={openGroups.has(group.key)}
              onToggleGroup={() => onToggleGroup(group.key)}
              expandedKey={expandedKey}
              onToggleRow={onToggleRow}
              newKeys={newKeys}
            />
          ))
        )}
      </div>

      {rawCount >= _MAX_ROWS ? (
        <p className="text-center text-xs text-gray-400">
          Showing the {_MAX_ROWS} most recent events per stream. Older events are not shown.
        </p>
      ) : null}
    </div>
  )
}
