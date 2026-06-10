'use client'

/**
 * VT-370 Gap-6 — Tenant Agents panel (client).
 *
 * Per-agent autonomy cards (level/streak/frozen/last_regression; a MISSING row renders the
 * L2/0/unfrozen default) with Freeze/Unfreeze + Demote/Revoke-L3 (confirm overlay with a
 * reason input — "no customer identifiers" helper, since scrub_pii catches digits, not names).
 * Below: the batch table (vtr_draft_batches aggregates — counts + template enums ONLY) with
 * per-batch Cancel (confirm + reason) and View drafts (rendered for everyone; the exception
 * tier is enforced server-side — a 403 renders gracefully). CL-390: nothing here logs
 * reasons, params, or response bodies.
 */

import { useState, useTransition } from 'react'
import { useRouter } from 'next/navigation'

import { useOverlay } from '@/components/ops/overlay-context'
import {
  autonomyOverrideAction,
  batchDraftsAction,
  cancelBatchAction,
} from '@/app/(app)/team/ops/tenants/[tenantId]/agents/actions'
import type {
  VtrAgentAutonomy,
  VtrDraftBatch,
  VtrOverrideAction,
} from '@/lib/orchestrator-client'

/** The specialist agents (OWNING_AGENTS minus 'unassigned') — cards render for ALL of these. */
const KNOWN_AGENTS = ['sales_recovery', 'reputation', 'acquisition', 'retention', 'menu_pricing']

function defaultState(tenantId: string, agent: string): VtrAgentAutonomy {
  return {
    tenant_id: tenantId,
    tenant_name: null,
    agent,
    level: 'L2',
    clean_approval_streak: 0,
    lifetime_approvals: 0,
    lifetime_rejections: 0,
    frozen: false,
    last_regression_at: null,
    last_regression_kind: null,
    l3_granted_at: null,
    l3_revoked_at: null,
    updated_at: null,
  }
}

export function AgentsPanel({
  tenantId,
  agents,
  batches,
}: {
  tenantId: string
  agents: VtrAgentAutonomy[]
  batches: VtrDraftBatch[]
}) {
  const overlay = useOverlay()
  const router = useRouter()
  const [pending, start] = useTransition()
  const [flash, setFlash] = useState<Record<string, string>>({})

  const byAgent = new Map(agents.map((a) => [a.agent, a]))
  const cards = KNOWN_AGENTS.map((name) => byAgent.get(name) ?? defaultState(tenantId, name))
  // Any agent the view knows that isn't in the known list still renders (forward-compatible).
  for (const a of agents) if (!KNOWN_AGENTS.includes(a.agent)) cards.push(a)

  function runOverride(agent: string, action: VtrOverrideAction, reason: string) {
    start(async () => {
      const res = await autonomyOverrideAction(tenantId, agent, action, reason)
      setFlash((f) => ({
        ...f,
        [agent]: res.ok
          ? `${action} ok${res.batchesCancelled ? ` (${res.batchesCancelled} batch(es) cancelled)` : ''}`
          : `${action} failed: ${res.reason}`,
      }))
      if (res.ok) router.refresh()
    })
  }

  /** Confirm overlay with a reason input for the destructive overrides. */
  function confirmOverride(agent: string, action: VtrOverrideAction, title: string) {
    overlay.open({
      key: `override-${agent}-${action}`,
      title,
      content: (
        <ReasonConfirm
          actionLabel={title}
          onConfirm={(reason) => {
            overlay.close()
            runOverride(agent, action, reason)
          }}
          onCancel={() => overlay.close()}
        />
      ),
    })
  }

  function cancelBatch(batchId: string) {
    overlay.open({
      key: `cancel-batch-${batchId}`,
      title: `Cancel batch ${batchId.slice(0, 8)}`,
      content: (
        <ReasonConfirm
          actionLabel="Cancel batch"
          onConfirm={(reason) => {
            overlay.close()
            start(async () => {
              const res = await cancelBatchAction(batchId, reason)
              setFlash((f) => ({
                ...f,
                [batchId]: res.ok
                  ? `cancelled (${res.draftsHalted} draft(s) halted)`
                  : `cancel failed: ${res.reason}`,
              }))
              if (res.ok) router.refresh()
            })
          }}
          onCancel={() => overlay.close()}
        />
      ),
    })
  }

  function viewDrafts(batch: VtrDraftBatch) {
    start(async () => {
      const res = await batchDraftsAction(batch.batch_id)
      overlay.open({
        key: `drafts-${batch.batch_id}`,
        title: `Drafts — batch ${batch.batch_id.slice(0, 8)}`,
        content: res.ok ? (
          <div data-ops-batch-drafts className="space-y-3 pt-2">
            <p className="text-xs text-gray-500">
              Exception-tier reveal — this read is audited (draft_params_reveal).
            </p>
            {res.drafts.length === 0 ? (
              <p data-ops-empty className="text-sm text-gray-600">
                No drafts in this batch.
              </p>
            ) : (
              <ul className="space-y-2">
                {res.drafts.map((d, i) => (
                  <li key={i} className="border border-gray-200 rounded p-2 text-sm text-gray-800">
                    <p>
                      <strong>{d.template_name}</strong> · {d.status}
                      {d.skip_reason ? ` · ${d.skip_reason}` : ''}
                    </p>
                    <pre className="text-xs text-gray-700 whitespace-pre-wrap break-all">
                      {JSON.stringify(d.params ?? {}, null, 2)}
                    </pre>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <p data-ops-batch-drafts-denied className="text-sm text-gray-700 pt-2">
            {res.reason === 'forbidden'
              ? 'Draft contents are exception-tier only (params can carry customer data). You see counts and template names; the drill-in is restricted and audited.'
              : `couldn't load drafts: ${res.reason}`}
          </p>
        ),
      })
    })
  }

  return (
    <>
      <section data-ops-agent-cards className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {cards.map((a) => (
          <article
            key={a.agent}
            data-ops-agent={a.agent}
            data-agent-frozen={a.frozen}
            className="bg-white rounded-lg shadow-sm border border-gray-200 p-4 space-y-2"
          >
            <header className="flex items-center justify-between">
              <h3 className="text-base font-semibold text-gray-900">{a.agent}</h3>
              <span
                className={`text-xs font-semibold px-2 py-0.5 rounded ${
                  a.frozen ? 'bg-red-100 text-red-800' : 'bg-gray-100 text-gray-700'
                }`}
              >
                {a.frozen ? 'FROZEN' : a.level}
              </span>
            </header>
            <ul className="text-sm text-gray-700 space-y-0.5">
              <li>level: {a.level}</li>
              <li>clean approval streak: {a.clean_approval_streak}</li>
              <li>
                lifetime: {a.lifetime_approvals} approvals / {a.lifetime_rejections} rejections
              </li>
              <li>
                last regression:{' '}
                {a.last_regression_kind
                  ? `${a.last_regression_kind} (${
                      a.last_regression_at ? new Date(a.last_regression_at).toLocaleString() : '—'
                    })`
                  : '—'}
              </li>
            </ul>
            <footer className="flex flex-wrap items-center gap-2 pt-1">
              {a.frozen ? (
                <button
                  type="button"
                  className="text-sm underline text-gray-800"
                  disabled={pending}
                  onClick={() => runOverride(a.agent, 'unfreeze', 'vtr console unfreeze')}
                >
                  Unfreeze
                </button>
              ) : (
                <button
                  type="button"
                  className="text-sm text-red-700 underline"
                  disabled={pending}
                  onClick={() => runOverride(a.agent, 'freeze', 'vtr console freeze')}
                >
                  Freeze
                </button>
              )}
              <button
                type="button"
                className="text-sm underline text-gray-800"
                disabled={pending}
                onClick={() => confirmOverride(a.agent, 'demote', `Demote ${a.agent} to L2`)}
              >
                Demote
              </button>
              <button
                type="button"
                className="text-sm underline text-gray-800"
                disabled={pending}
                onClick={() => confirmOverride(a.agent, 'revoke_l3', `Revoke L3 — ${a.agent}`)}
              >
                Revoke L3
              </button>
              {flash[a.agent] && (
                <span className="text-xs text-gray-600" data-agent-flash>
                  {flash[a.agent]}
                </span>
              )}
            </footer>
          </article>
        ))}
      </section>

      <section
        data-ops-draft-batches
        className="bg-white rounded-lg shadow-sm border border-gray-200 p-4 space-y-2"
      >
        <h2 className="text-base font-semibold text-gray-900">Draft batches</h2>
        {batches.length === 0 ? (
          <p data-ops-empty className="text-sm text-gray-600">
            No draft batches for this tenant.
          </p>
        ) : (
          <table className="w-full text-sm text-gray-700">
            <thead>
              <tr className="text-left text-xs text-gray-500">
                <th>Batch</th>
                <th>Agent</th>
                <th>Status</th>
                <th>Pending</th>
                <th>Sent</th>
                <th>Skipped</th>
                <th>Halted</th>
                <th>Templates</th>
                <th>Created</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {batches.map((b) => (
                <tr key={b.batch_id} data-batch-id={b.batch_id} data-batch-status={b.status}>
                  <td className="font-mono text-xs">{b.batch_id.slice(0, 8)}</td>
                  <td>{b.agent}</td>
                  <td>{flash[b.batch_id] ?? b.status}</td>
                  <td>{b.pending_count}</td>
                  <td>{b.sent_count}</td>
                  <td>{b.skipped_count}</td>
                  <td>{b.halted_count}</td>
                  <td className="text-xs">{b.template_names.filter(Boolean).join(', ') || '—'}</td>
                  <td className="text-xs">
                    {b.created_at ? new Date(b.created_at).toLocaleString() : '—'}
                  </td>
                  <td className="space-x-2">
                    <button
                      type="button"
                      className="text-sm text-red-700 underline"
                      disabled={
                        pending ||
                        b.status === 'cancelled' ||
                        b.status === 'completed' ||
                        !!flash[b.batch_id]
                      }
                      onClick={() => cancelBatch(b.batch_id)}
                    >
                      Cancel
                    </button>
                    {/* Rendered for everyone; the exception tier is enforced (and audited)
                        server-side — a 403 shows the graceful message in the overlay. */}
                    <button
                      type="button"
                      className="text-sm underline text-gray-800"
                      disabled={pending}
                      onClick={() => viewDrafts(b)}
                    >
                      View drafts
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  )
}

/** Reason + confirm body for destructive overrides / batch cancel. */
function ReasonConfirm({
  actionLabel,
  onConfirm,
  onCancel,
}: {
  actionLabel: string
  onConfirm: (reason: string) => void
  onCancel: () => void
}) {
  const [reason, setReason] = useState('')
  return (
    <form
      data-ops-reason-confirm
      className="space-y-3 pt-2"
      onSubmit={(e) => {
        e.preventDefault()
        onConfirm(reason)
      }}
    >
      <label className="block text-sm text-gray-700 space-y-1">
        <span>Reason</span>
        <textarea
          className="w-full border border-gray-300 rounded px-2 py-1 text-sm text-gray-900"
          rows={3}
          maxLength={500}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </label>
      <p className="text-xs text-gray-500">
        Do not include customer names or identifiers in reasons — automated scrubbing removes
        numbers, not names.
      </p>
      <div className="flex gap-2">
        <button
          type="submit"
          className="text-sm border border-red-300 rounded px-3 py-1 bg-red-50 text-red-800"
        >
          {actionLabel}
        </button>
        <button type="button" className="text-sm underline text-gray-600" onClick={onCancel}>
          Back
        </button>
      </div>
    </form>
  )
}
