'use client'

/**
 * VT-376 (Phase C) — run-control interactive controls (the WRITE layer over Phase-B's read
 * canvas). Dialogs + buttons that drive the five server actions in ./actions.ts. EVERY mutating
 * path goes through requireOpsOperator-gated server actions; the orchestrator re-derives tenant
 * + re-checks the assignment gate + audits BEFORE the mutation. The panel invents NO new
 * mutation paths.
 *
 * Binding honesty disclosures wired here (row §2 — the honesty layer):
 *   - keys-only blind-write warning on the override dialog (editing values you cannot see)
 *   - consume-failure (F-5): "a pin may silently not apply on control outage — check the
 *     timeline after the run"
 *   - rerun I2 banner: "outputs re-enter owner approval"
 *   - rerun PRE-FLIGHT: open approval ⇒ warn + disabled submit (server 409 is the authority)
 *   - escalated_overlap (C1-A): the overlap-escalation outcome rendered prominently post-rerun
 *   - "reason is redacted at write" note on pause/override reason inputs
 *   - non-rerunnable kinds: no button + the per-kind forbidden_reason why-copy
 *   - observed steps: NO controls (the read canvas already badges them; we render no button)
 *
 * CL-390: nothing here logs reasons, pins, override ids, or response bodies.
 */

import { useEffect, useState, useTransition } from 'react'
import { useRouter } from 'next/navigation'

import { useOverlay } from '@/components/ops/overlay-context'
import type { VtrTimelineStep } from '@/lib/orchestrator-client'

import {
  cancelOverrideAction,
  overrideAction,
  pauseAction,
  rerunAction,
  rerunPreflightAction,
  releaseAction,
} from './actions'

// ── Binding copy (verbatim — the e2e spec asserts several of these) ──────────
export const RC_BLIND_WRITE_COPY =
  'Keys only — you are editing values you cannot see.'
export const RC_CONSUME_FAIL_COPY =
  'If control reads degrade, a pin may silently not apply — check the timeline after the run.'
export const RC_RERUN_I2_COPY = 'Re-running re-enters owner approval — outputs are not auto-kept.'
export const RC_RERUN_PREFLIGHT_WARN =
  'Owner approval pending — rerun will refuse. Resolve the approval first.'
// Single literal — the e2e bundle assertion greps the served JS verbatim; '+'-concat only
// survives if the minifier constant-folds, which the binding copy check must not ride on.
export const RC_ESCALATED_OVERLAP_COPY =
  'An owner approval armed during this re-run — the run was ESCALATED, not silently kept. Check the escalation queue.'
export const RC_REASON_REDACTED_COPY = 'Reason is redacted at write (customer names are stripped).'

/** Map the per-kind forbidden_reason annotation to the operator-facing why-copy (verbatim). */
const FORBIDDEN_WHY: Record<string, string> = {
  'message-dedup semantics':
    'Not re-runnable — re-dispatching an inbound message would break message-dedup semantics.',
  'duplicate-nudge risk':
    'Not re-runnable — re-running the trial sweep risks sending a duplicate nudge.',
  'kg-duplication':
    'Not re-runnable — re-sending a campaign would duplicate knowledge-graph entries.',
}

export function forbiddenWhyCopy(forbiddenReason: string | null): string {
  if (forbiddenReason && FORBIDDEN_WHY[forbiddenReason]) return FORBIDDEN_WHY[forbiddenReason]
  return 'Not re-runnable — re-dispatch is disabled for this workflow kind.'
}

// ───────────────────────────────────────────────────────────────────────────
// Pause / release — tenant × kind tiles
// ───────────────────────────────────────────────────────────────────────────

/** Confirm + optional reason for a pause. */
function PauseConfirm({
  onConfirm,
  onCancel,
}: {
  onConfirm: (reason: string) => void
  onCancel: () => void
}) {
  const [reason, setReason] = useState('')
  return (
    <form
      data-rc-pause-confirm
      className="space-y-3 pt-2"
      onSubmit={(e) => {
        e.preventDefault()
        onConfirm(reason)
      }}
    >
      <p className="text-sm text-foreground">
        Pause this workflow for the tenant. In-flight steps finish; new arms of this kind are held
        until you release.
      </p>
      <label className="block text-sm text-foreground space-y-1">
        <span>Reason (optional)</span>
        <textarea
          className="w-full border border-input rounded px-2 py-1 text-sm text-foreground"
          rows={3}
          maxLength={500}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </label>
      <p className="text-xs text-muted-foreground">{RC_REASON_REDACTED_COPY}</p>
      <div className="flex gap-2">
        <button
          type="submit"
          className="text-sm border border-gold/40 rounded px-3 py-1 bg-gold/15 text-gold-foreground"
        >
          Pause
        </button>
        <button type="button" className="text-sm underline text-muted-foreground" onClick={onCancel}>
          Back
        </button>
      </div>
    </form>
  )
}

export function PauseReleaseControls({
  tenantId,
  workflowKind,
  paused,
}: {
  tenantId: string
  workflowKind: string
  paused: boolean
}) {
  const overlay = useOverlay()
  const router = useRouter()
  const [pending, start] = useTransition()
  const [flash, setFlash] = useState<string | null>(null)

  function doPause(reason: string) {
    start(async () => {
      const res = await pauseAction(tenantId, workflowKind, reason)
      setFlash(res.ok ? 'paused' : `pause failed: ${res.reason}`)
      if (res.ok) router.refresh()
    })
  }
  function doRelease() {
    start(async () => {
      const res = await releaseAction(tenantId, workflowKind)
      setFlash(res.ok ? 'released' : `release failed: ${res.reason}`)
      if (res.ok) router.refresh()
    })
  }

  return (
    <span data-rc-pause-release className="inline-flex items-center gap-2">
      {paused ? (
        <button
          type="button"
          data-rc-release-btn
          className="text-xs underline text-foreground"
          disabled={pending}
          onClick={doRelease}
        >
          Release
        </button>
      ) : (
        <button
          type="button"
          data-rc-pause-btn
          className="text-xs underline text-gold-foreground"
          disabled={pending}
          onClick={() =>
            overlay.open({
              key: `pause-${tenantId}-${workflowKind}`,
              title: `Pause ${workflowKind}`,
              content: (
                <PauseConfirm
                  onConfirm={(reason) => {
                    overlay.close()
                    doPause(reason)
                  }}
                  onCancel={() => overlay.close()}
                />
              ),
            })
          }
        >
          Pause
        </button>
      )}
      {flash && (
        <span data-rc-pause-flash className="text-[10px] text-muted-foreground">
          {flash}
        </span>
      )}
    </span>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Override — controllable steps ONLY
// ───────────────────────────────────────────────────────────────────────────

const NEXT_RUN_DEFAULT_EXPIRY_DAYS = 7

/** One field per allowed_key — the operator types a VALUE blind (names redacted at write). */
function OverrideDialog({
  step,
  onSubmit,
  onCancel,
}: {
  step: VtrTimelineStep
  onSubmit: (pins: Record<string, unknown>, reason: string) => void
  onCancel: () => void
}) {
  const keys = step.allowed_keys ?? []
  const [values, setValues] = useState<Record<string, string>>({})
  const [reason, setReason] = useState('')

  return (
    <form
      data-rc-override-dialog
      className="space-y-3 pt-2"
      onSubmit={(e) => {
        e.preventDefault()
        // Only non-empty fields become pins (an untouched key is left unpinned).
        const pins: Record<string, unknown> = {}
        for (const k of keys) {
          const v = values[k]
          if (v !== undefined && v !== '') pins[k] = v
        }
        onSubmit(pins, reason)
      }}
    >
      {/* BLIND-WRITE warning — the operator edits values they cannot see (I7). */}
      <p data-rc-blind-write className="text-xs font-medium text-gold-foreground bg-gold/15 border border-gold/40 rounded p-2">
        {RC_BLIND_WRITE_COPY}
      </p>
      {/* CONSUME-FAILURE disclosure (F-5) — visible near the dialog. */}
      <p data-rc-consume-fail className="text-xs text-muted-foreground">
        {RC_CONSUME_FAIL_COPY}
      </p>
      {keys.length === 0 ? (
        <p className="text-sm text-muted-foreground">This step pins no keys.</p>
      ) : (
        <div className="space-y-2">
          {keys.map((k) => (
            <label key={k} className="block text-sm text-foreground space-y-1">
              <span className="font-mono text-xs">{k}</span>
              <input
                type="text"
                data-rc-override-key={k}
                className="w-full border border-input rounded px-2 py-1 text-sm text-foreground font-mono"
                value={values[k] ?? ''}
                onChange={(e) => setValues((v) => ({ ...v, [k]: e.target.value }))}
              />
            </label>
          ))}
        </div>
      )}
      <label className="block text-sm text-foreground space-y-1">
        <span>Reason (optional)</span>
        <textarea
          className="w-full border border-input rounded px-2 py-1 text-sm text-foreground"
          rows={2}
          maxLength={500}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </label>
      <p className="text-xs text-muted-foreground">{RC_REASON_REDACTED_COPY}</p>
      <p className="text-[11px] text-muted-foreground">
        Next-run pins expire in {NEXT_RUN_DEFAULT_EXPIRY_DAYS} days by default.
      </p>
      <div className="flex gap-2">
        <button
          type="submit"
          className="text-sm border border-primary/40 rounded px-3 py-1 bg-primary/10 text-primary"
        >
          Pin override
        </button>
        <button type="button" className="text-sm underline text-muted-foreground" onClick={onCancel}>
          Back
        </button>
      </div>
    </form>
  )
}

export function OverrideControl({
  step,
  workflowKind,
  tenantId,
  workflowId,
}: {
  step: VtrTimelineStep
  workflowKind: string
  tenantId: string
  /** the run id, for a row-targeted pin (omit ⇒ next-run, tenant-scoped). */
  workflowId?: string | null
}) {
  const overlay = useOverlay()
  const router = useRouter()
  const [pending, start] = useTransition()
  const [flash, setFlash] = useState<string | null>(null)

  // Override controls render ONLY for controllable steps — observed steps get no button at all.
  if (step.tier !== 'controllable' || !step.step_name) return null

  function submit(pins: Record<string, unknown>, reason: string) {
    start(async () => {
      // Next-run pins default to a 7-day expiry (the server requires a future value when no
      // workflowId is targeted); row-targeted pins consume on the next arm and carry no expiry.
      const expiresAt = workflowId
        ? null
        : new Date(Date.now() + NEXT_RUN_DEFAULT_EXPIRY_DAYS * 86_400_000).toISOString()
      const res = await overrideAction({
        tenantId,
        workflowKind,
        stepName: step.step_name!,
        workflowId: workflowId ?? null,
        pinnedInput: Object.keys(pins).length ? pins : null,
        reason,
        expiresAt,
      })
      setFlash(
        res.ok
          ? 'override pinned'
          : `override failed: ${res.reason}${res.detail.length ? ` (${res.detail.join('; ')})` : ''}`,
      )
      if (res.ok) router.refresh()
    })
  }

  return (
    <span data-rc-override-control className="inline-flex items-center gap-2">
      <button
        type="button"
        data-rc-override-btn
        className="text-xs underline text-primary"
        disabled={pending}
        onClick={() =>
          overlay.open({
            key: `override-${step.step_id ?? step.step_name}`,
            title: `Override ${step.step_name}`,
            content: (
              <OverrideDialog
                step={step}
                onSubmit={(pins, reason) => {
                  overlay.close()
                  submit(pins, reason)
                }}
                onCancel={() => overlay.close()}
              />
            ),
          })
        }
      >
        Override
      </button>
      {step.override_id && (
        <CancelOverrideControl overrideId={step.override_id} />
      )}
      {flash && (
        <span data-rc-override-flash className="text-[10px] text-muted-foreground">
          {flash}
        </span>
      )}
    </span>
  )
}

/** Cancel a PENDING (unconsumed) override pin — ROW-targeted (tenant derived server-side). */
export function CancelOverrideControl({ overrideId }: { overrideId: string }) {
  const router = useRouter()
  const [pending, start] = useTransition()
  const [flash, setFlash] = useState<string | null>(null)
  return (
    <span data-rc-cancel-override className="inline-flex items-center gap-1">
      <button
        type="button"
        data-rc-cancel-override-btn
        className="text-xs underline text-muted-foreground"
        disabled={pending || flash === 'cancelled'}
        onClick={() =>
          start(async () => {
            const res = await cancelOverrideAction(overrideId)
            setFlash(res.ok ? 'cancelled' : `cancel failed: ${res.reason}`)
            if (res.ok) router.refresh()
          })
        }
      >
        Cancel pin
      </button>
      {flash && <span className="text-[10px] text-muted-foreground">{flash}</span>}
    </span>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Rerun — rerunnable runs ONLY; non-rerunnable show why-copy
// ───────────────────────────────────────────────────────────────────────────

/** Rerun confirm: I2 banner + PRE-FLIGHT open-approval re-fetch (open ⇒ warn + disabled submit). */
function RerunConfirm({
  runId,
  fromStep,
  onSubmit,
  onCancel,
}: {
  runId: string
  fromStep: string
  onSubmit: () => void
  onCancel: () => void
}) {
  // Start in the "checking" state and re-fetch open-approval on mount. Fail-safe: a failed
  // pre-flight read leaves openApproval=true (warn + blocked) rather than implying "clear".
  const [openApproval, setOpenApproval] = useState<boolean | null>(null)
  const [, start] = useTransition()

  // Kick the pre-flight once on mount (server action — re-fetches the live open-approval state).
  useEffect(() => {
    start(async () => {
      const res = await rerunPreflightAction(runId)
      setOpenApproval(res.openApproval)
    })
    // runId is the only input; the effect runs once per dialog open.
  }, [runId])

  const blocked = openApproval !== false // null (still checking) or true ⇒ submit disabled

  return (
    <div data-rc-rerun-confirm className="space-y-3 pt-2">
      {/* I2 — outputs re-enter owner approval. */}
      <p data-rc-rerun-i2 className="text-xs font-medium text-foreground bg-muted/40 border border-border rounded p-2">
        {RC_RERUN_I2_COPY}
      </p>
      <p className="text-sm text-foreground">
        Re-dispatch run <span className="font-mono text-xs">{runId}</span> from{' '}
        <span className="font-mono text-xs">{fromStep}</span>. This starts a NEW run (no
        time-travel); prior steps re-execute only if the entry point requires them.
      </p>
      {openApproval === null ? (
        <p data-rc-rerun-preflight-checking className="text-xs text-muted-foreground">
          Checking owner-approval state…
        </p>
      ) : openApproval ? (
        <p data-rc-rerun-preflight-warn className="text-xs font-medium text-destructive bg-destructive/10 border border-destructive/30 rounded p-2">
          {RC_RERUN_PREFLIGHT_WARN}
        </p>
      ) : (
        <p data-rc-rerun-preflight-clear className="text-xs text-muted-foreground">
          No owner approval pending.
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          data-rc-rerun-submit
          className="text-sm border border-primary/40 rounded px-3 py-1 bg-primary/10 text-primary disabled:opacity-40"
          disabled={blocked}
          onClick={onSubmit}
        >
          Re-run
        </button>
        <button type="button" className="text-sm underline text-muted-foreground" onClick={onCancel}>
          Back
        </button>
      </div>
    </div>
  )
}

export function RerunControl({
  runId,
  fromStep,
  rerunnable,
  forbiddenReason,
}: {
  runId: string
  /** the step to re-dispatch from (the run's entry/first controllable step). */
  fromStep: string
  rerunnable: boolean
  forbiddenReason: string | null
}) {
  const overlay = useOverlay()
  const router = useRouter()
  const [pending, start] = useTransition()
  const [result, setResult] = useState<
    { kind: 'ok' | 'overlap' | 'err'; text: string } | null
  >(null)

  // Non-rerunnable kinds: NO button — the per-kind why-copy from forbidden_reason instead.
  if (!rerunnable) {
    return (
      <span data-rc-rerun-forbidden className="text-[11px] italic text-muted-foreground">
        {forbiddenWhyCopy(forbiddenReason)}
      </span>
    )
  }

  function doRerun() {
    start(async () => {
      const res = await rerunAction(runId, fromStep, [])
      if (!res.ok) {
        setResult({
          kind: 'err',
          text: `rerun failed: ${res.reason}${res.detail.length ? ` (${res.detail.join('; ')})` : ''}`,
        })
        return
      }
      // C1-A: 'escalated_overlap' is still a SUCCESS (the rerun ran) — surface the disclosure
      // PROMINENTLY rather than as a quiet success.
      if (res.outcome === 'escalated_overlap') {
        setResult({ kind: 'overlap', text: RC_ESCALATED_OVERLAP_COPY })
      } else {
        setResult({ kind: 'ok', text: `re-dispatched as ${res.newRunId ?? 'a new run'}` })
      }
      router.refresh()
    })
  }

  return (
    <span data-rc-rerun-control className="inline-flex flex-col gap-1">
      <button
        type="button"
        data-rc-rerun-btn
        className="text-xs underline text-primary"
        disabled={pending}
        onClick={() =>
          overlay.open({
            key: `rerun-${runId}`,
            title: 'Re-run',
            content: (
              <RerunConfirm
                runId={runId}
                fromStep={fromStep}
                onSubmit={() => {
                  overlay.close()
                  doRerun()
                }}
                onCancel={() => overlay.close()}
              />
            ),
          })
        }
      >
        Re-run
      </button>
      {result?.kind === 'overlap' && (
        <span
          data-rc-rerun-escalated-overlap
          className="text-[11px] font-medium text-destructive bg-destructive/10 border border-destructive/30 rounded px-1.5 py-0.5"
        >
          {result.text}
        </span>
      )}
      {result?.kind === 'ok' && (
        <span data-rc-rerun-ok className="text-[10px] text-muted-foreground">
          {result.text}
        </span>
      )}
      {result?.kind === 'err' && (
        <span data-rc-rerun-err className="text-[10px] text-destructive">
          {result.text}
        </span>
      )}
    </span>
  )
}
