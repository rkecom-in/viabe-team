'use client'

/**
 * VT-517 — the VTR ownership-review surface (the interactive panel on the per-tenant ops page).
 *
 * Self-serve ownership OTP is gone: a Viabe human decides ownership here. The operator records a
 * decision (verified | rejected) with a free-text note + evidence; verifyOwnershipAction resolves the
 * operator server-side (the panel never sends operator_id as trusted scope) and the orchestrator
 * re-checks the assignment behind the action. On success the route refreshes so the panel disappears
 * (ownership_status leaves 'pending'). Light-mode only (hardcoded colors, no dark: variants).
 */

import { useState, useTransition } from 'react'
import { useRouter } from 'next/navigation'

import { verifyOwnershipAction } from '@/app/(app)/team/ops/tenants/[tenantId]/actions'

export function OwnershipDecisionPanel({
  tenantId,
  operatorId,
}: {
  tenantId: string
  operatorId: string
}) {
  const router = useRouter()
  const [pending, startTransition] = useTransition()
  const [note, setNote] = useState('')
  const [evidence, setEvidence] = useState('')
  const [error, setError] = useState<string | null>(null)

  function decide(decision: 'verified' | 'rejected') {
    setError(null)
    startTransition(async () => {
      const r = await verifyOwnershipAction(tenantId, decision, note.trim(), evidence.trim())
      if (r.ok) {
        router.refresh()
      } else {
        setError(r.reason)
      }
    })
  }

  return (
    <section
      data-ownership-decision
      data-operator-id={operatorId}
      className="rounded-lg border border-amber-300 bg-amber-50 p-6 shadow-sm"
    >
      <h2 className="text-lg font-semibold text-amber-900">Ownership review</h2>
      <p className="mt-1 text-sm text-amber-800">
        This business is awaiting a human ownership decision. The AI agent will not act on its
        customers until ownership is marked verified.
      </p>

      <label className="mt-4 block text-sm font-medium text-gray-700" htmlFor="ownership-note">
        Note
      </label>
      <textarea
        id="ownership-note"
        data-ownership-note
        rows={2}
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="What you checked, and why this decision."
        className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 outline-none focus:border-amber-500"
      />

      <label className="mt-3 block text-sm font-medium text-gray-700" htmlFor="ownership-evidence">
        Evidence
      </label>
      <input
        id="ownership-evidence"
        data-ownership-evidence
        type="text"
        value={evidence}
        onChange={(e) => setEvidence(e.target.value)}
        placeholder="e.g. a link or reference to what you verified."
        className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 outline-none focus:border-amber-500"
      />

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          type="button"
          data-ownership-verify
          onClick={() => decide('verified')}
          disabled={pending}
          className="rounded-md border border-green-300 bg-green-50 px-3 py-1.5 text-sm font-medium text-green-700 hover:bg-green-100 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {pending ? 'Saving…' : 'Mark Verified'}
        </button>
        <button
          type="button"
          data-ownership-reject
          onClick={() => decide('rejected')}
          disabled={pending}
          className="rounded-md border border-red-300 bg-red-50 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-100 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {pending ? 'Saving…' : 'Mark Rejected'}
        </button>
        {error && (
          <span data-ownership-error className="text-xs text-red-600">
            {error === 'forbidden'
              ? 'not allowed'
              : error === 'not_found'
                ? 'tenant not found'
                : error === 'conflict'
                  ? 'already decided'
                  : "couldn't save"}
          </span>
        )}
      </div>
    </section>
  )
}
