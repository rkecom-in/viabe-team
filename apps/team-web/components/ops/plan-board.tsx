'use client'

/**
 * VT-370 Gap-6 — Tenant Plan board (client).
 *
 * Seq-ordered roadmap cards + metadata history sidebar. Per-item: Edit (overlay modal,
 * EDITABLE_FIELDS only), Drop (status=dropped, two-step confirm), Move to month N (a `month`
 * patch — NO reorder UI; `seq` is seam-immutable, true reorder is a deferred post-launch seam).
 * Every mutation sends `expected_prev_version` from the LOADED plan: 409 → "plan changed,
 * reload"; 400 → the server-scrubbed grounding violations (render-only — never logged, CL-390).
 */

import { useState, useTransition } from 'react'
import { useRouter } from 'next/navigation'

import { useOverlay } from '@/components/ops/overlay-context'
import { editRoadmapItemAction } from '@/app/(app)/team/ops/tenants/[tenantId]/plan/actions'
import type { VtrPlan, VtrPlanHistoryEntry, VtrRoadmapItem } from '@/lib/orchestrator-client'

const MONTHS = [1, 2, 3, 4, 5, 6]
const STATUSES = ['proposed', 'accepted', 'in_progress', 'done', 'dropped']
const OWNING_AGENTS = [
  'sales_recovery',
  'reputation',
  'acquisition',
  'retention',
  'menu_pricing',
  'unassigned',
]

function staleMessage(reason: string): string {
  if (reason === 'stale_version') return 'plan changed, reload the page and retry'
  return `failed: ${reason}`
}

export function PlanBoard({
  tenantId,
  plan,
  history,
}: {
  tenantId: string
  plan: VtrPlan | null
  history: VtrPlanHistoryEntry[]
}) {
  const overlay = useOverlay()
  const router = useRouter()
  const [pending, start] = useTransition()
  const [flash, setFlash] = useState<Record<string, string>>({})
  const [confirmDrop, setConfirmDrop] = useState<string | null>(null)
  const [moveTo, setMoveTo] = useState<Record<string, number>>({})

  if (!plan) {
    return (
      <section className="flex gap-6 items-start" data-ops-plan-empty-wrap>
        <p data-ops-empty className="flex-1 text-sm text-gray-600">
          No business plan yet for this tenant.
        </p>
        <HistorySidebar history={history} />
      </section>
    )
  }

  const items = [...plan.roadmap_json].sort((a, b) => a.seq - b.seq)

  function patchItem(item: VtrRoadmapItem, patch: Record<string, unknown>, okLabel: string) {
    start(async () => {
      const res = await editRoadmapItemAction(tenantId, item.item_id, patch, plan!.version)
      if (res.ok) {
        setFlash((f) => ({ ...f, [item.item_id]: okLabel }))
        router.refresh()
      } else {
        setFlash((f) => ({ ...f, [item.item_id]: staleMessage(res.reason) }))
      }
    })
  }

  function openEdit(item: VtrRoadmapItem) {
    overlay.open({
      key: `plan-edit-${item.item_id}`,
      title: `Edit item (month ${item.month})`,
      content: (
        <EditItemForm
          tenantId={tenantId}
          item={item}
          expectedPrevVersion={plan!.version}
        />
      ),
    })
  }

  return (
    <section className="flex gap-6 items-start" data-ops-plan>
      <div className="flex-1 space-y-4" data-ops-plan-cards>
        {items.length === 0 && <p data-ops-empty>The latest plan has no roadmap items.</p>}
        {items.map((item) => (
          <article
            key={item.item_id}
            data-ops-plan-item={item.item_id}
            data-item-status={item.status}
            className="bg-white rounded-lg shadow-sm border border-gray-200 p-4 space-y-2"
          >
            <header className="flex items-center justify-between">
              <span className="text-xs font-semibold text-gray-500">
                Month {item.month} · seq {item.seq}
              </span>
              <span className="text-xs text-gray-600" data-item-meta>
                {item.owning_agent} · {item.status}
              </span>
            </header>
            <h3 className="text-base font-semibold text-gray-900">{item.objective}</h3>
            <p className="text-sm text-gray-700">{item.why}</p>
            {item.owner_action_needed && (
              <p className="text-sm text-gray-700" data-item-owner-action>
                <strong>Owner action:</strong> {item.owner_action ?? '—'}
                {item.owner_action_hi ? ` / ${item.owner_action_hi}` : ''}
              </p>
            )}
            <footer className="flex items-center gap-2 pt-1">
              <button
                type="button"
                className="text-sm underline text-gray-800"
                disabled={pending}
                onClick={() => openEdit(item)}
              >
                Edit
              </button>
              {confirmDrop === item.item_id ? (
                <>
                  <button
                    type="button"
                    className="text-sm text-red-700 underline"
                    disabled={pending}
                    onClick={() => {
                      setConfirmDrop(null)
                      patchItem(item, { status: 'dropped' }, 'dropped')
                    }}
                  >
                    Confirm drop
                  </button>
                  <button
                    type="button"
                    className="text-sm underline text-gray-600"
                    onClick={() => setConfirmDrop(null)}
                  >
                    Keep
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="text-sm underline text-gray-800"
                  disabled={pending || item.status === 'dropped'}
                  onClick={() => setConfirmDrop(item.item_id)}
                >
                  Drop
                </button>
              )}
              <label className="text-sm text-gray-700 ml-2">
                Move to month{' '}
                <select
                  className="border border-gray-300 rounded px-1"
                  value={moveTo[item.item_id] ?? item.month}
                  onChange={(e) =>
                    setMoveTo((m) => ({ ...m, [item.item_id]: Number(e.target.value) }))
                  }
                >
                  {MONTHS.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="text-sm underline text-gray-800"
                disabled={pending || (moveTo[item.item_id] ?? item.month) === item.month}
                onClick={() =>
                  patchItem(
                    item,
                    { month: moveTo[item.item_id] ?? item.month },
                    `moved to month ${moveTo[item.item_id] ?? item.month}`,
                  )
                }
              >
                Move
              </button>
              {flash[item.item_id] && (
                <span className="text-xs text-gray-600" data-item-flash>
                  {flash[item.item_id]}
                </span>
              )}
            </footer>
          </article>
        ))}
      </div>
      <HistorySidebar history={history} />
    </section>
  )
}

/** Metadata-only history (vtr_plan_history view: version/generated_by/model_id/created_at). */
function HistorySidebar({ history }: { history: VtrPlanHistoryEntry[] }) {
  return (
    <aside
      data-ops-plan-history
      className="w-72 shrink-0 bg-white rounded-lg shadow-sm border border-gray-200 p-4"
    >
      <h2 className="text-sm font-semibold text-gray-900 mb-2">Version history</h2>
      {history.length === 0 ? (
        <p data-ops-empty className="text-sm text-gray-600">
          No versions yet.
        </p>
      ) : (
        <table className="w-full text-xs text-gray-700">
          <thead>
            <tr className="text-left text-gray-500">
              <th>v</th>
              <th>by</th>
              <th>when</th>
            </tr>
          </thead>
          <tbody>
            {[...history]
              .sort((a, b) => b.version - a.version)
              .map((h) => (
                <tr key={h.version}>
                  <td>{h.version}</td>
                  <td className="break-all">{h.generated_by}</td>
                  <td>{h.created_at ? new Date(h.created_at).toLocaleString() : '—'}</td>
                </tr>
              ))}
          </tbody>
        </table>
      )}
    </aside>
  )
}

/** Edit modal body — EDITABLE_FIELDS only; sends the changed keys + expected_prev_version. */
function EditItemForm({
  tenantId,
  item,
  expectedPrevVersion,
}: {
  tenantId: string
  item: VtrRoadmapItem
  expectedPrevVersion: number
}) {
  const overlay = useOverlay()
  const router = useRouter()
  const [pending, start] = useTransition()
  const [error, setError] = useState<string | null>(null)
  const [violations, setViolations] = useState<string[]>([])
  const [form, setForm] = useState({
    objective: item.objective,
    why: item.why,
    month: item.month,
    owner_action: item.owner_action ?? '',
    owner_action_hi: item.owner_action_hi ?? '',
    owner_action_needed: item.owner_action_needed,
    status: item.status,
    owning_agent: item.owning_agent,
  })

  function changedPatch(): Record<string, unknown> {
    const patch: Record<string, unknown> = {}
    if (form.objective !== item.objective) patch.objective = form.objective
    if (form.why !== item.why) patch.why = form.why
    if (form.month !== item.month) patch.month = form.month
    if (form.owner_action !== (item.owner_action ?? '')) {
      patch.owner_action = form.owner_action || null
    }
    if (form.owner_action_hi !== (item.owner_action_hi ?? '')) {
      patch.owner_action_hi = form.owner_action_hi || null
    }
    if (form.owner_action_needed !== item.owner_action_needed) {
      patch.owner_action_needed = form.owner_action_needed
    }
    if (form.status !== item.status) patch.status = form.status
    if (form.owning_agent !== item.owning_agent) patch.owning_agent = form.owning_agent
    return patch
  }

  function submit() {
    const patch = changedPatch()
    if (Object.keys(patch).length === 0) {
      setError('nothing changed')
      return
    }
    setError(null)
    setViolations([])
    start(async () => {
      const res = await editRoadmapItemAction(tenantId, item.item_id, patch, expectedPrevVersion)
      if (res.ok) {
        overlay.close()
        router.refresh()
        return
      }
      if (res.reason === 'stale_version') {
        setError('plan changed, reload the page and retry')
      } else if (res.reason === 'grounding_or_patch') {
        setError('edit rejected (grounding/patch validation)')
        setViolations(res.violations)
      } else {
        setError(`failed: ${res.reason}`)
      }
    })
  }

  const label = 'block text-sm text-gray-700 space-y-1'
  const input = 'w-full border border-gray-300 rounded px-2 py-1 text-sm text-gray-900'

  return (
    <form
      data-ops-plan-edit-form
      className="space-y-3 pt-2"
      onSubmit={(e) => {
        e.preventDefault()
        submit()
      }}
    >
      <label className={label}>
        <span>Objective</span>
        <textarea
          className={input}
          rows={2}
          value={form.objective}
          onChange={(e) => setForm((f) => ({ ...f, objective: e.target.value }))}
        />
      </label>
      <label className={label}>
        <span>Why</span>
        <textarea
          className={input}
          rows={3}
          value={form.why}
          onChange={(e) => setForm((f) => ({ ...f, why: e.target.value }))}
        />
      </label>
      <label className={label}>
        <span>Month</span>
        <select
          className={input}
          value={form.month}
          onChange={(e) => setForm((f) => ({ ...f, month: Number(e.target.value) }))}
        >
          {MONTHS.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </label>
      <label className={label}>
        <span>Owner action</span>
        <input
          className={input}
          value={form.owner_action}
          onChange={(e) => setForm((f) => ({ ...f, owner_action: e.target.value }))}
        />
      </label>
      <label className={label}>
        <span>Owner action (Hindi)</span>
        <input
          className={input}
          value={form.owner_action_hi}
          onChange={(e) => setForm((f) => ({ ...f, owner_action_hi: e.target.value }))}
        />
      </label>
      <label className="flex items-center gap-2 text-sm text-gray-700">
        <input
          type="checkbox"
          checked={form.owner_action_needed}
          onChange={(e) => setForm((f) => ({ ...f, owner_action_needed: e.target.checked }))}
        />
        Owner action needed
      </label>
      <label className={label}>
        <span>Status</span>
        <select
          className={input}
          value={form.status}
          onChange={(e) => setForm((f) => ({ ...f, status: e.target.value }))}
        >
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>
      <label className={label}>
        <span>Owning agent</span>
        <select
          className={input}
          value={form.owning_agent}
          onChange={(e) => setForm((f) => ({ ...f, owning_agent: e.target.value }))}
        >
          {OWNING_AGENTS.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
      </label>

      {error && (
        <p data-ops-plan-edit-error className="text-sm text-red-700">
          {error}
        </p>
      )}
      {violations.length > 0 && (
        <ul data-ops-plan-edit-violations className="text-xs text-red-700 list-disc pl-4">
          {violations.map((v, i) => (
            <li key={i}>{v}</li>
          ))}
        </ul>
      )}

      <div className="flex gap-2 pt-1">
        <button
          type="submit"
          disabled={pending}
          className="text-sm border border-gray-300 rounded px-3 py-1 bg-gray-50 text-gray-900"
        >
          {pending ? 'Saving…' : 'Save edit'}
        </button>
        <button
          type="button"
          className="text-sm underline text-gray-600"
          onClick={() => overlay.close()}
        >
          Cancel
        </button>
      </div>
      <p className="text-xs text-gray-500">
        Edits re-ground against the plan&apos;s frozen fact bundle and mint a new version
        (v{expectedPrevVersion} → v{expectedPrevVersion + 1}).
      </p>
    </form>
  )
}
