'use client'

/**
 * VT-201 Ops live-stream feed (PR-1 minimum-viable).
 *
 * Client-side Supabase Realtime subscription to pipeline_steps INSERTs;
 * incoming rows prepend to the visible feed (bounded to 200 rows in
 * memory to avoid browser bloat). Filter sidebar + quick-filter pills
 * toggle inline.
 *
 * Per Q5 Option A locked at plan-review: tenant business_name cached
 * client-side after first sight; reduces JOIN cost.
 */

import { useCallback, useEffect, useState } from 'react'

import {
  browserStreamClient,
  subscribePipelineSteps,
  type PipelineStepEvent,
  type StreamFilters,
} from '@/lib/ops/stream'
import { StreamRowList } from '@/components/ops/stream-row-list'

const _MAX_ROWS_IN_MEMORY = 200

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

export interface TenantOption {
  tenant_id: string
  business_name: string | null
}

export interface StreamFeedProps {
  operatorJwt: string
  availableTenants: TenantOption[]
}

export function StreamFeed({ operatorJwt, availableTenants }: StreamFeedProps) {
  const [rows, setRows] = useState<PipelineStepEvent[]>([])
  const [filters, setFilters] = useState<StreamFilters>({})
  const [connected, setConnected] = useState(false)
  const [tenantNames, setTenantNames] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {}
    for (const t of availableTenants) {
      if (t.business_name) init[t.tenant_id] = t.business_name
    }
    return init
  })

  const onEvent = useCallback((step: PipelineStepEvent) => {
    setRows((prev) => [step, ...prev].slice(0, _MAX_ROWS_IN_MEMORY))
  }, [])

  useEffect(() => {
    let unsubscribe: (() => void) | null = null
    let cancelled = false
    queueMicrotask(() => {
      if (cancelled) return
      try {
        const client = browserStreamClient(operatorJwt)
        unsubscribe = subscribePipelineSteps(client, filters, onEvent)
        setConnected(true)
      } catch (err) {
        console.error('VT-201 stream subscribe failed', err)
        setConnected(false)
      }
    })
    return () => {
      cancelled = true
      unsubscribe?.()
      setConnected(false)
    }
  }, [operatorJwt, filters, onEvent])

  const tenantName = useCallback(
    (tenantId: string): string => tenantNames[tenantId] ?? tenantId,
    [tenantNames],
  )

  // Suppress unused warning if tenant name learning logic gets added later.
  void setTenantNames

  return (
    <div className="ops-stream-feed-grid" data-component="stream-feed">
      <FilterSidebar
        availableTenants={availableTenants}
        filters={filters}
        onChange={setFilters}
      />
      <QuickFilterPills filters={filters} onChange={setFilters} />
      <section data-section="connection-status">
        Realtime: {connected ? 'connected' : 'disconnected'}
      </section>
      <StreamRowList rows={rows} tenantName={tenantName} />
    </div>
  )
}


interface FilterSidebarProps {
  availableTenants: TenantOption[]
  filters: StreamFilters
  onChange: (f: StreamFilters) => void
}

function FilterSidebar({ availableTenants, filters, onChange }: FilterSidebarProps) {
  return (
    <aside data-component="filter-sidebar">
      <section>
        <h3>Tenant</h3>
        {availableTenants.map((t) => {
          const checked = !!filters.tenantIds?.includes(t.tenant_id)
          return (
            <label key={t.tenant_id} data-filter="tenant">
              <input
                type="checkbox"
                checked={checked}
                onChange={() => {
                  const cur = filters.tenantIds ?? []
                  const next = checked
                    ? cur.filter((id) => id !== t.tenant_id)
                    : [...cur, t.tenant_id]
                  onChange({ ...filters, tenantIds: next })
                }}
              />
              {t.business_name ?? t.tenant_id}
            </label>
          )
        })}
      </section>
      <section>
        <h3>Step kind</h3>
        {_STEP_KIND_OPTIONS.map((k) => {
          const checked = !!filters.stepKinds?.includes(k)
          return (
            <label key={k} data-filter="step-kind">
              <input
                type="checkbox"
                checked={checked}
                onChange={() => {
                  const cur = filters.stepKinds ?? []
                  const next = checked
                    ? cur.filter((x) => x !== k)
                    : [...cur, k]
                  onChange({ ...filters, stepKinds: next })
                }}
              />
              {k}
            </label>
          )
        })}
      </section>
      <section>
        <h3>Status</h3>
        {_STATUS_OPTIONS.map((s) => {
          const checked = !!filters.statuses?.includes(s)
          return (
            <label key={s} data-filter="status">
              <input
                type="checkbox"
                checked={checked}
                onChange={() => {
                  const cur = filters.statuses ?? []
                  const next = checked
                    ? cur.filter((x) => x !== s)
                    : [...cur, s]
                  onChange({ ...filters, statuses: next })
                }}
              />
              {s}
            </label>
          )
        })}
      </section>
    </aside>
  )
}


interface QuickFilterPillsProps {
  filters: StreamFilters
  onChange: (f: StreamFilters) => void
}

function QuickFilterPills({ filters, onChange }: QuickFilterPillsProps) {
  const onlyFailures = filters.statuses?.length === 1 && filters.statuses[0] === 'failed'
  const onlyEscalations = filters.stepKinds?.length === 1 && filters.stepKinds[0] === 'aborted_hard_limit'

  return (
    <div data-component="quick-filter-pills">
      <button
        type="button"
        data-pill="failures-only"
        aria-pressed={onlyFailures}
        onClick={() =>
          onChange(
            onlyFailures
              ? { ...filters, statuses: undefined }
              : { ...filters, statuses: ['failed'] },
          )
        }
      >
        {onlyFailures ? '✓ ' : ''}failures only
      </button>
      <button
        type="button"
        data-pill="escalations-only"
        aria-pressed={onlyEscalations}
        onClick={() =>
          onChange(
            onlyEscalations
              ? { ...filters, stepKinds: undefined }
              : { ...filters, stepKinds: ['aborted_hard_limit'] },
          )
        }
      >
        {onlyEscalations ? '✓ ' : ''}escalations only
      </button>
      <button
        type="button"
        data-pill="clear"
        onClick={() => onChange({})}
      >
        clear filters
      </button>
    </div>
  )
}
