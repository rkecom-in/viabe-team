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

/** VT-89 Razorpay-event forward result. */
export interface RazorpayForwardResult {
  /** True when the orchestrator DURABLY recorded the event (2xx). The webhook
   * returns 200 to Razorpay only when this is true (Q1 — else 5xx so Razorpay
   * retries; never silently drop a financial event). */
  ok: boolean
  /** duplicate | processed | ignored | http_<n> | timeout | error */
  status: string
}

/**
 * Forward a verified Razorpay event to the orchestrator's razorpay-ingress (the
 * durable inbox + sole writer of fee state + phase transitions), signed with
 * INTERNAL_API_SECRET. ``ok`` reflects whether the orchestrator returned 2xx — the
 * caller (the webhook route) returns 200 to Razorpay ONLY when ok, else 5xx so the
 * event is retried (Q1 financial durability). Never throws.
 */
export async function forwardRazorpayEvent(
  eventId: string,
  eventType: string,
  payload: Record<string, unknown>,
): Promise<RazorpayForwardResult> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
  const secret = process.env.INTERNAL_API_SECRET ?? ''
  try {
    const res = await fetch(`${base}/api/orchestrator/razorpay-ingress`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'X-Internal-Secret': secret },
      body: JSON.stringify({ event_id: eventId, event_type: eventType, payload }),
      signal: AbortSignal.timeout(_FORWARD_TIMEOUT_MS),
    })
    if (!res.ok) return { ok: false, status: `http_${res.status}` }
    const data = (await res.json()) as { status?: string }
    return { ok: true, status: data.status ?? 'unknown' }
  } catch (err) {
    const timedOut = err instanceof Error && err.name === 'TimeoutError'
    return { ok: false, status: timedOut ? 'timeout' : 'error' }
  }
}

/** VT-300 run-control forward result. */
export interface RunControlForwardResult {
  ok: boolean
  /** ok | http_403 (not assigned) | http_404 (run gone) | http_<n> | timeout | error */
  reason: string
}

/**
 * Forward a VTR run-control (pause/steer/override) to the orchestrator's authoritative endpoint.
 * The orchestrator RE-DERIVES the run's tenant + RE-CHECKS operator_assignments server-side
 * (team-web auth is fail-open at the enforcement leg) and audits. NO tenant crosses the wire —
 * only run_id + operator_id + control_type. Never throws.
 */
export async function forwardRunControl(
  operatorId: string,
  runId: string,
  controlType: string,
  directive?: string,
): Promise<RunControlForwardResult> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
  const secret = process.env.INTERNAL_API_SECRET ?? ''
  try {
    const res = await fetch(`${base}/api/orchestrator/ops/run-control`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'X-Internal-Secret': secret },
      body: JSON.stringify({
        run_id: runId,
        operator_id: operatorId,
        control_type: controlType,
        directive: directive ?? null,
      }),
      signal: AbortSignal.timeout(_FORWARD_TIMEOUT_MS),
    })
    if (!res.ok) return { ok: false, reason: `http_${res.status}` }
    return { ok: true, reason: 'ok' }
  } catch (err) {
    const timedOut = err instanceof Error && err.name === 'TimeoutError'
    return { ok: false, reason: timedOut ? 'timeout' : 'error' }
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
