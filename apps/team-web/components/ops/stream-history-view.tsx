'use client'

/**
 * VT-201 PR-2 — historical stream view client component.
 *
 * Date picker (default today IST) + hour scrubber + filter panel +
 * play button at 1x / 10x / 100x. Reuses StreamRowList for row
 * rendering so the DOM shape matches the live feed (PR-1).
 *
 * Cowork lock (2026-05-28): hour-scoped fetches (not full-day) — keeps
 * memory under control on busy days. Scrubber jumps trigger a fresh
 * hour fetch.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { StreamRowList } from '@/components/ops/stream-row-list'
import type { PipelineStepEvent } from '@/lib/ops/stream'
import type { TenantOption } from '@/components/ops/stream-feed'

const _STEP_KIND_OPTIONS = [
  'webhook_received',
  'agent_invocation',
  'agent_reasoning_step',
  'mcp_tool_call',
  'state_transition',
  'compose_output',
  'aborted_hard_limit',
  'l0_write',
  'l0_query',
  'message_dispatch',
] as const

const _STATUS_OPTIONS = ['completed', 'running', 'failed', 'skipped'] as const

interface HistoryFilters {
  tenantIds: string[]
  stepKinds: string[]
  statuses: string[]
  q: string
}

export interface StreamHistoryViewProps {
  initialDate: string  // YYYY-MM-DD IST
  availableTenants: TenantOption[]
}

const PLAY_SPEEDS = [
  { label: '1x', value: 1 },
  { label: '10x', value: 10 },
  { label: '100x', value: 100 },
] as const

function todayIst(): string {
  const now = new Date()
  const ist = new Date(now.getTime() + (5 * 60 + 30) * 60_000)
  return ist.toISOString().slice(0, 10)
}

export function StreamHistoryView({ initialDate, availableTenants }: StreamHistoryViewProps) {
  const [date, setDate] = useState(initialDate || todayIst())
  const [hour, setHour] = useState(0)
  const [filters, setFilters] = useState<HistoryFilters>({
    tenantIds: [], stepKinds: [], statuses: [], q: '',
  })
  const [rows, setRows] = useState<PipelineStepEvent[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [replayIdx, setReplayIdx] = useState<number | null>(null)
  const [replaySpeed, setReplaySpeed] = useState<number>(1)
  const [tenantNames] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {}
    for (const t of availableTenants) {
      if (t.business_name) init[t.tenant_id] = t.business_name
    }
    return init
  })
  const replayTimerRef = useRef<number | null>(null)

  const tenantName = useCallback(
    (tenantId: string): string => tenantNames[tenantId] ?? tenantId,
    [tenantNames],
  )

  const fetchPage = useCallback(
    async (cursor: string | null) => {
      setLoading(true)
      setError(null)
      try {
        const sp = new URLSearchParams({ date, hour: String(hour) })
        if (cursor) sp.set('cursor', cursor)
        if (filters.tenantIds.length) sp.set('tenant_ids', filters.tenantIds.join(','))
        if (filters.stepKinds.length) sp.set('step_kinds', filters.stepKinds.join(','))
        if (filters.statuses.length) sp.set('statuses', filters.statuses.join(','))
        if (filters.q.trim()) sp.set('q', filters.q.trim())
        const res = await fetch(`/api/ops/history?${sp.toString()}`)
        if (!res.ok) {
          throw new Error(`http_${res.status}`)
        }
        const data = (await res.json()) as { rows: PipelineStepEvent[]; next_cursor: string | null }
        return data
      } finally {
        setLoading(false)
      }
    },
    [date, hour, filters],
  )

  // Fetch first page on date / hour / filter change. The setState
  // happens asynchronously after the fetch resolves — the lint rule
  // (react-hooks/set-state-in-effect) is conservative here; the
  // setState IS the legitimate effect (data → UI).
  useEffect(() => {
    let cancelled = false
    /* eslint-disable react-hooks/set-state-in-effect */
    fetchPage(null)
      .then((data) => {
        if (cancelled) return
        const asc = [...data.rows].reverse()
        setRows(asc)
        setNextCursor(data.next_cursor)
        setReplayIdx(null)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : String(err))
      })
    /* eslint-enable react-hooks/set-state-in-effect */
    return () => {
      cancelled = true
    }
  }, [fetchPage])

  const loadMore = useCallback(() => {
    if (!nextCursor || loading) return
    fetchPage(nextCursor)
      .then((data) => {
        const asc = [...data.rows].reverse()
        setRows((prev) => [...asc, ...prev])
        setNextCursor(data.next_cursor)
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
  }, [fetchPage, nextCursor, loading])

  // Replay timer: when replayIdx is non-null, advance through rows at
  // the configured speed using the actual started_at deltas / speed.
  useEffect(() => {
    if (replayIdx === null || rows.length === 0) return
    if (replayIdx >= rows.length - 1) return
    const here = rows[replayIdx]
    const next = rows[replayIdx + 1]
    if (!here || !next) return
    const deltaMs = new Date(next.started_at).getTime() - new Date(here.started_at).getTime()
    const tickMs = Math.max(50, Math.round(deltaMs / replaySpeed))
    replayTimerRef.current = window.setTimeout(() => {
      setReplayIdx((idx) => (idx === null ? null : idx + 1))
    }, tickMs)
    return () => {
      if (replayTimerRef.current !== null) {
        window.clearTimeout(replayTimerRef.current)
        replayTimerRef.current = null
      }
    }
  }, [replayIdx, rows, replaySpeed])

  const visibleRows = useMemo(() => {
    if (replayIdx === null) return rows
    return rows.slice(0, replayIdx + 1)
  }, [rows, replayIdx])

  const startReplay = useCallback(() => {
    setReplayIdx(0)
  }, [])

  const stopReplay = useCallback(() => {
    setReplayIdx(null)
  }, [])

  return (
    <div className="ops-stream-history" data-component="stream-history">
      <header data-section="controls">
        <label>
          Date (IST)
          <input
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            data-element="date-picker"
          />
        </label>
        <label>
          Hour
          <input
            type="range"
            min={0}
            max={23}
            value={hour}
            onChange={(e) => setHour(Number(e.target.value))}
            data-element="hour-scrubber"
          />
          <span data-element="hour-label">{String(hour).padStart(2, '0')}:00</span>
        </label>
        <div data-section="replay-controls">
          {replayIdx === null ? (
            <button onClick={startReplay} data-element="play">
              Play
            </button>
          ) : (
            <button onClick={stopReplay} data-element="stop">
              Stop
            </button>
          )}
          {PLAY_SPEEDS.map((s) => (
            <button
              key={s.value}
              onClick={() => setReplaySpeed(s.value)}
              aria-pressed={replaySpeed === s.value}
              data-element={`speed-${s.value}`}
            >
              {s.label}
            </button>
          ))}
        </div>
      </header>

      <aside data-section="filters">
        <fieldset>
          <legend>Tenants</legend>
          {availableTenants.map((t) => (
            <label key={t.tenant_id}>
              <input
                type="checkbox"
                checked={filters.tenantIds.includes(t.tenant_id)}
                onChange={(e) =>
                  setFilters((f) => ({
                    ...f,
                    tenantIds: e.target.checked
                      ? [...f.tenantIds, t.tenant_id]
                      : f.tenantIds.filter((x) => x !== t.tenant_id),
                  }))
                }
              />
              {t.business_name ?? t.tenant_id}
            </label>
          ))}
        </fieldset>
        <fieldset>
          <legend>Step kinds</legend>
          {_STEP_KIND_OPTIONS.map((k) => (
            <label key={k}>
              <input
                type="checkbox"
                checked={filters.stepKinds.includes(k)}
                onChange={(e) =>
                  setFilters((f) => ({
                    ...f,
                    stepKinds: e.target.checked
                      ? [...f.stepKinds, k]
                      : f.stepKinds.filter((x) => x !== k),
                  }))
                }
              />
              {k}
            </label>
          ))}
        </fieldset>
        <fieldset>
          <legend>Statuses</legend>
          {_STATUS_OPTIONS.map((s) => (
            <label key={s}>
              <input
                type="checkbox"
                checked={filters.statuses.includes(s)}
                onChange={(e) =>
                  setFilters((f) => ({
                    ...f,
                    statuses: e.target.checked
                      ? [...f.statuses, s]
                      : f.statuses.filter((x) => x !== s),
                  }))
                }
              />
              {s}
            </label>
          ))}
        </fieldset>
        <fieldset>
          <legend>Free-text</legend>
          <input
            type="text"
            value={filters.q}
            onChange={(e) => setFilters((f) => ({ ...f, q: e.target.value }))}
            placeholder="search envelopes…"
            data-element="free-text"
          />
        </fieldset>
      </aside>

      <section data-section="status">
        {loading && <span data-element="loading">Loading…</span>}
        {error && <span data-element="error">Error: {error}</span>}
        <span data-element="row-count">{visibleRows.length} of {rows.length}</span>
      </section>

      <StreamRowList rows={visibleRows} tenantName={tenantName} />

      {nextCursor && (
        <button onClick={loadMore} data-element="load-more">
          Load older
        </button>
      )}
    </div>
  )
}
