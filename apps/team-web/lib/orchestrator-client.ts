/** Result of forwarding an inbound webhook to the orchestrator. */
export interface ForwardResult {
  /** True when the orchestrator returned 2xx (regardless of its `reason`). */
  ok: boolean
  /** The DBOS workflow id, or null (unknown sender / rate limited / error). */
  workflowId: string | null
  /** started | dupe | unknown_sender | rate_limit_exceeded | error_logged | timeout | http_<n> */
  reason: string
}

const _ORCHESTRATOR_DEFAULT = 'http://localhost:8001'
const _FORWARD_TIMEOUT_MS = 5000

/**
 * Forward raw Twilio fields to the orchestrator's ingress endpoint, signed
 * with INTERNAL_API_SECRET. The orchestrator resolves the tenant, rate-limits,
 * and starts the DBOS workflow — it returns immediately after the start, well
 * within the 5s budget. Never throws — the caller (Pillar 7) must not 5xx.
 */
export async function forwardToOrchestrator(
  twilioFields: Record<string, string>,
): Promise<ForwardResult> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
  const secret = process.env.INTERNAL_API_SECRET ?? ''

  try {
    const res = await fetch(`${base}/api/orchestrator/twilio-ingress`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-Internal-Secret': secret,
      },
      body: JSON.stringify({ twilio_fields: twilioFields }),
      signal: AbortSignal.timeout(_FORWARD_TIMEOUT_MS),
    })
    if (!res.ok) {
      return { ok: false, workflowId: null, reason: `http_${res.status}` }
    }
    const data = (await res.json()) as { workflow_id: string | null; reason: string }
    return { ok: true, workflowId: data.workflow_id, reason: data.reason }
  } catch (err) {
    const timedOut = err instanceof Error && err.name === 'TimeoutError'
    return { ok: false, workflowId: null, reason: timedOut ? 'timeout' : 'error' }
  }
}

/** VT-211 onboard-step result envelope. */
export interface OnboardStepResult {
  ok: boolean
  /** When ok=true: the agent's next phase. */
  nextPhase: string | null
  /** When ok=true: the agent's next prompt for the owner (rendered on page reload). */
  nextPrompt: string | null
  /** Always present — http_<n> | timeout | error | tenant_not_configured | server_error. */
  reason: string
}

const _ONBOARD_STEP_TIMEOUT_MS = 30_000

/**
 * Forward an owner's onboarding answer to the orchestrator. The orchestrator
 * invokes the integration_agent subgraph directly (NOT the supervisor — no
 * brain pass needed for an explicit web-driven step; per VT-211 Cowork
 * correction 2026-05-28). Persists phase transitions; returns the next
 * prompt text the page should render.
 *
 * Never throws — callers (Pillar 7) must not 5xx.
 */
export async function forwardOnboardStep(
  tenantId: string,
  answer: string,
): Promise<OnboardStepResult> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
  const secret = process.env.INTERNAL_API_SECRET ?? ''
  try {
    const res = await fetch(
      `${base}/api/orchestrator/integrations/onboard-step`,
      {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'X-Internal-Secret': secret,
        },
        body: JSON.stringify({ tenant_id: tenantId, answer }),
        signal: AbortSignal.timeout(_ONBOARD_STEP_TIMEOUT_MS),
      },
    )
    if (!res.ok) {
      return { ok: false, nextPhase: null, nextPrompt: null, reason: `http_${res.status}` }
    }
    const data = (await res.json()) as {
      next_phase?: string | null
      next_prompt?: string | null
    }
    return {
      ok: true,
      nextPhase: data.next_phase ?? null,
      nextPrompt: data.next_prompt ?? null,
      reason: 'ok',
    }
  } catch (err) {
    const timedOut = err instanceof Error && err.name === 'TimeoutError'
    return {
      ok: false,
      nextPhase: null,
      nextPrompt: null,
      reason: timedOut ? 'timeout' : 'error',
    }
  }
}
