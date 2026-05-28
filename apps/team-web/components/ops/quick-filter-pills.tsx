/**
 * VT-201 PR-3 — shared quick-filter pills.
 *
 * Extracted from `stream-feed.tsx` so both the live stream and the
 * history view render the same pills. Tenant pill uses a single-select
 * dropdown per Cowork Q5 (cycling fails past 3 tenants).
 *
 * Per CL-417: filter state is a plain `StreamFilters` shape, no
 * sub-state — the parent owns the filter object and feeds it to both
 * the row query and these pills.
 */

import type { StreamFilters } from '@/lib/ops/stream'
import type { TenantOption } from '@/components/ops/stream-feed'

export interface QuickFilterPillsProps {
  filters: StreamFilters
  availableTenants?: TenantOption[]
  onChange: (f: StreamFilters) => void
}

export function QuickFilterPills({
  filters,
  availableTenants,
  onChange,
}: QuickFilterPillsProps) {
  const onlyFailures =
    filters.statuses?.length === 1 && filters.statuses[0] === 'failed'
  const onlyEscalations =
    filters.stepKinds?.length === 1 &&
    filters.stepKinds[0] === 'aborted_hard_limit'
  const activeTenantId = (filters.tenantIds ?? [])[0] ?? ''
  const activeTenant = availableTenants?.find(
    (t) => t.tenant_id === activeTenantId,
  )

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
      {availableTenants && (
        <label data-pill="active-tenant">
          tenant:
          <select
            value={activeTenantId}
            onChange={(e) =>
              onChange(
                e.target.value === ''
                  ? { ...filters, tenantIds: undefined }
                  : { ...filters, tenantIds: [e.target.value] },
              )
            }
          >
            <option value="">all</option>
            {availableTenants.map((t) => (
              <option key={t.tenant_id} value={t.tenant_id}>
                {t.business_name ?? t.tenant_id}
              </option>
            ))}
          </select>
          {activeTenant && (
            <span data-pill-active-label>
              {activeTenant.business_name ?? activeTenant.tenant_id}
            </span>
          )}
        </label>
      )}
      <button type="button" data-pill="clear" onClick={() => onChange({})}>
        clear filters
      </button>
    </div>
  )
}
