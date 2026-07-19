'use client'

/**
 * VT-556 — VTR strategy/behavioural directive ingest form (human-as-teacher input).
 *
 * The VTR teaches the Team Manager: a directive lands in agent_memory with provenance +
 * authority='vtr' and is marked retrieval-eligible, so the manager picks it up on its next run
 * (gated server-side by MANAGER_MEMORY_RETRIEVAL). Distinct from the plan-edit draft correction.
 * operator_id is server-derived (requireOpsOperator in the action); this form sends only the text.
 */

import { useState, useTransition } from 'react'

import { ingestDirectiveAction } from '@/app/(app)/team/ops/tenants/[tenantId]/actions'

export function DirectiveForm({ tenantId }: { tenantId: string }) {
  const [pending, start] = useTransition()
  const [kind, setKind] = useState<'strategy' | 'behavioural'>('strategy')
  const [key, setKey] = useState('')
  const [content, setContent] = useState('')
  const [flash, setFlash] = useState<string | null>(null)

  function submit() {
    const k = key.trim()
    const c = content.trim()
    if (!k || !c) {
      setFlash('memory key and directive text are required')
      return
    }
    start(async () => {
      const res = await ingestDirectiveAction(tenantId, k, c, kind)
      if (res.ok) {
        setFlash(`directive ingested (v${res.version ?? '?'}) — the manager picks it up next run`)
        setContent('')
      } else {
        const extra = res.violations.length ? ` — ${res.violations.join('; ')}` : ''
        setFlash(`failed: ${res.reason}${extra}`)
      }
    })
  }

  return (
    <section
      className="bg-card rounded-lg shadow-sm border border-border p-6 space-y-3"
      data-section="vtr-directive"
    >
      <h2 className="text-lg font-semibold text-foreground">Ingest strategy / behavioural directive</h2>
      <p className="text-xs text-muted-foreground">
        Teach the Team Manager. Human-as-teacher input — the manager reads active directives on its
        next run (not a draft correction). Provenance + authority are recorded; do not include
        customer PII.
      </p>
      <div className="flex items-center gap-2">
        <label className="text-sm text-foreground" htmlFor="directive-kind">
          kind
        </label>
        <select
          id="directive-kind"
          className="rounded border border-border bg-background px-2 py-1 text-sm text-foreground"
          value={kind}
          onChange={(e) => setKind(e.target.value as 'strategy' | 'behavioural')}
          data-field="directive-kind"
        >
          <option value="strategy">strategy</option>
          <option value="behavioural">behavioural</option>
        </select>
      </div>
      <input
        className="w-full rounded border border-border bg-background px-2 py-1 text-sm text-foreground"
        placeholder="memory key (stable id, e.g. winback_tone)"
        value={key}
        onChange={(e) => setKey(e.target.value)}
        data-field="directive-key"
      />
      <textarea
        className="w-full rounded border border-border bg-background px-2 py-1 text-sm text-foreground"
        rows={3}
        placeholder="directive text — e.g. Prioritise dormant high-value customers; keep the tone warm and concise."
        value={content}
        onChange={(e) => setContent(e.target.value)}
        data-field="directive-content"
      />
      <button
        type="button"
        className="rounded bg-primary px-3 py-1 text-sm text-primary-foreground disabled:opacity-50"
        disabled={pending}
        onClick={submit}
        data-action="ingest-directive"
      >
        {pending ? 'ingesting…' : 'Ingest directive'}
      </button>
      {flash && (
        <p className="text-sm text-muted-foreground" data-directive-flash>
          {flash}
        </p>
      )}
    </section>
  )
}
