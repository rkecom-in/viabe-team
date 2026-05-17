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
