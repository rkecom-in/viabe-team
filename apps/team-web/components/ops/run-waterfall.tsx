'use client'

/**
 * VT-123 run-replay waterfall. Renders pipeline_steps rows as a vertical
 * step list with native canonical columns (CL-417) — parent_step_id,
 * tokens_input/output, status, model_used, tool_calls, step_name,
 * decision_rationale, step_seq — NOT JSONB extraction at view time.
 *
 * Per CL-417: every column has its own JSX node (`data-col=*`) so the
 * Playwright canary can assert their presence per step row.
 *
 * Per CL-390 + VT-188: the [resolve] button on any step whose envelope
 * contains a `phone_token` reveals the decrypted phone via the
 * `/api/ops/resolve-phone` proxy route. Every resolve emits an audit
 * row inside the stored function's transaction (VT-188 substrate).
 */

import { useState } from 'react'

import type { PipelineStepRow } from '@/lib/ops/data-access'

export interface RunWaterfallProps {
  steps: PipelineStepRow[]
}

export function RunWaterfall({ steps }: RunWaterfallProps) {
  return (
    <ol className="ops-run-waterfall" data-component="run-waterfall">
      {steps.map((step) => (
        <StepCard key={step.id} step={step} />
      ))}
    </ol>
  )
}

function StepCard({ step }: { step: PipelineStepRow }) {
  const [expanded, setExpanded] = useState(false)
  const phoneToken = _findPhoneToken(step)

  return (
    <li className="ops-step-card" data-step-seq={step.step_seq} data-step-kind={step.step_kind}>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        data-action="toggle-expand"
      >
        <span data-col="step_seq">#{step.step_seq}</span>{' '}
        <span data-col="step_kind">{step.step_kind}</span>{' '}
        <span data-col="step_name">{step.step_name ?? '—'}</span>{' '}
        <span data-col="status">{step.status}</span>
      </button>

      {expanded && (
        <div data-section="step-detail">
          <dl>
            <div>
              <dt>parent_step_id</dt>
              <dd data-col="parent_step_id">{step.parent_step_id ?? '—'}</dd>
            </div>
            <div>
              <dt>decision_rationale</dt>
              <dd data-col="decision_rationale">{step.decision_rationale ?? '—'}</dd>
            </div>
            <div>
              <dt>model_used</dt>
              <dd data-col="model_used">{step.model_used ?? '—'}</dd>
            </div>
            <div>
              <dt>tokens_input</dt>
              <dd data-col="tokens_input">{step.tokens_input ?? '—'}</dd>
            </div>
            <div>
              <dt>tokens_output</dt>
              <dd data-col="tokens_output">{step.tokens_output ?? '—'}</dd>
            </div>
            <div>
              <dt>cost_paise</dt>
              <dd data-col="cost_paise">{step.cost_paise ?? '—'}</dd>
            </div>
            <div>
              <dt>duration_ms</dt>
              <dd data-col="duration_ms">{step.duration_ms ?? '—'}</dd>
            </div>
            <div>
              <dt>tool_calls</dt>
              <dd data-col="tool_calls">
                <pre>{JSON.stringify(step.tool_calls, null, 2)}</pre>
              </dd>
            </div>
          </dl>

          <section data-section="envelopes">
            <h3>input_envelope</h3>
            <pre data-col="input_envelope">
              {JSON.stringify(step.input_envelope, null, 2)}
            </pre>
            <h3>output_envelope</h3>
            <pre data-col="output_envelope">
              {JSON.stringify(step.output_envelope, null, 2)}
            </pre>
            {step.error ? (
              <>
                <h3>error</h3>
                <pre data-col="error">{JSON.stringify(step.error, null, 2)}</pre>
              </>
            ) : null}
          </section>

          <section data-section="actions">
            {phoneToken ? (
              <ResolveButton phoneToken={phoneToken} stepId={step.id} />
            ) : null}
            <ExportFixtureButton step={step} />
          </section>
        </div>
      )}
    </li>
  )
}

function ResolveButton({
  phoneToken,
  stepId,
}: {
  phoneToken: string
  stepId: string
}) {
  const [status, setStatus] = useState<'idle' | 'pending' | 'revealed' | 'error'>('idle')
  const [phone, setPhone] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function onResolve() {
    if (!confirm(`Reveal phone for token ${phoneToken}? Every reveal is audit-logged.`)) {
      return
    }
    setStatus('pending')
    try {
      const res = await fetch('/api/ops/resolve-phone', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ phone_token: phoneToken, step_id: stepId }),
      })
      if (!res.ok) {
        const body = await res.text()
        setStatus('error')
        setError(`HTTP ${res.status}: ${body}`)
        return
      }
      const data = (await res.json()) as { phone_e164: string | null }
      setPhone(data.phone_e164)
      setStatus('revealed')
    } catch (err) {
      setStatus('error')
      setError((err as Error).message)
    }
  }

  return (
    <div data-action="resolve">
      <button type="button" onClick={onResolve} disabled={status === 'pending'}>
        {status === 'pending' ? '...' : 'resolve phone'}
      </button>
      {status === 'revealed' ? (
        <span data-resolved-phone>{phone ?? '(no row)'}</span>
      ) : null}
      {status === 'error' ? <span data-resolved-error>{error}</span> : null}
    </div>
  )
}

function ExportFixtureButton({ step }: { step: PipelineStepRow }) {
  const [copied, setCopied] = useState(false)
  async function onCopy() {
    const payload = JSON.stringify(step.input_envelope, null, 2)
    await navigator.clipboard.writeText(payload)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div data-action="export-fixture">
      <button type="button" onClick={onCopy}>
        {copied ? 'copied' : 'export step as test fixture'}
      </button>
    </div>
  )
}

function _findPhoneToken(step: PipelineStepRow): string | null {
  const candidates = [step.input_envelope, step.output_envelope]
  for (const env of candidates) {
    const token = _searchPhoneToken(env)
    if (token) return token
  }
  return null
}

function _searchPhoneToken(value: unknown): string | null {
  if (typeof value === 'string') {
    return value.startsWith('phone_tok_') ? value : null
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const t = _searchPhoneToken(item)
      if (t) return t
    }
    return null
  }
  if (value && typeof value === 'object') {
    for (const v of Object.values(value as Record<string, unknown>)) {
      const t = _searchPhoneToken(v)
      if (t) return t
    }
  }
  return null
}
