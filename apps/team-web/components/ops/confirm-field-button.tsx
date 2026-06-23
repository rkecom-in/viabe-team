'use client'

/**
 * VT-405 Part B — per-row Confirm button (the interactive cell in the otherwise-server discovery
 * panel). Calls the scoped confirmFieldAction (server-side scope resolution); on success refreshes
 * the route so the field re-renders as VTR-asserted. Confirmed values never reach this component —
 * it knows only the field NAME + booleans (PII boundary).
 */

import { useState, useTransition } from 'react'
import { useRouter } from 'next/navigation'

import { confirmFieldAction } from '@/app/(app)/team/ops/tenants/[tenantId]/actions'

export function ConfirmFieldButton({ tenantId, field }: { tenantId: string; field: string }) {
  const router = useRouter()
  const [pending, startTransition] = useTransition()
  const [error, setError] = useState<string | null>(null)

  function onConfirm() {
    setError(null)
    startTransition(async () => {
      const r = await confirmFieldAction(tenantId, field)
      if (r.ok) {
        router.refresh()
      } else {
        setError(r.reason)
      }
    })
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        type="button"
        onClick={onConfirm}
        disabled={pending}
        data-confirm-field={field}
        className="rounded border border-blue-300 bg-blue-50 px-2 py-0.5 text-xs font-medium text-blue-700 hover:bg-blue-100 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {pending ? 'Confirming…' : 'Confirm'}
      </button>
      {error && (
        <span data-confirm-error className="text-xs text-red-600">
          {error === 'forbidden'
            ? 'not allowed'
            : error === 'not_found'
              ? 'no value'
              : error === 'invalid_field'
                ? 'invalid'
                : "couldn't confirm"}
        </span>
      )}
    </span>
  )
}
