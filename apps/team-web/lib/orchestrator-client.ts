import { issueOperatorJwt, OPERATOR_RESOLVE_TTL_SEC } from '@/lib/auth/operator-jwt'

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

/** VT-331 Razorpay-subscribe forward result. */
export interface SubscribeForwardResult {
  /** True when the orchestrator returned 2xx (subscription created or already existed). */
  ok: boolean
  /** created | exists | http_<n> | timeout | error */
  status: string
  razorpaySubscriptionId: string | null
}

/**
 * Forward a subscription-create to the orchestrator's razorpay-subscribe — the
 * money-authoritative layer (it resolves plan_tier -> plan_id/amount, makes the Razorpay
 * vendor call, writes subscriptions). team-web sends only {tenant_id (server-derived),
 * plan_tier} — never a resolved plan_id/amount (Cowork Q1). Never throws.
 */
export async function forwardSubscribe(
  tenantId: string,
  planTier: string,
  jti: string | null = null,
): Promise<SubscribeForwardResult> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
  const secret = process.env.INTERNAL_API_SECRET ?? ''
  try {
    const res = await fetch(`${base}/api/orchestrator/razorpay-subscribe`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'X-Internal-Secret': secret },
      // VT-332: forward jti ONLY on the trial-end token path (the orchestrator consumes it
      // single-use). Omitted on the in-app path (no token → no jti).
      body: JSON.stringify({ tenant_id: tenantId, plan_tier: planTier, ...(jti ? { jti } : {}) }),
      signal: AbortSignal.timeout(_FORWARD_TIMEOUT_MS),
    })
    if (!res.ok) return { ok: false, status: `http_${res.status}`, razorpaySubscriptionId: null }
    const data = (await res.json()) as { status?: string; razorpay_subscription_id?: string }
    return {
      ok: true,
      status: data.status ?? 'unknown',
      razorpaySubscriptionId: data.razorpay_subscription_id ?? null,
    }
  } catch (err) {
    const timedOut = err instanceof Error && err.name === 'TimeoutError'
    return { ok: false, status: timedOut ? 'timeout' : 'error', razorpaySubscriptionId: null }
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


// ---------------------------------------------------------------------------
// VT-360 — VTR de-identified reads via the orchestrator (fork C).
//
// CL-425 is DB-ENFORCED on the team-web VTR surface here: instead of reading raw tables with the
// service-role client + app-side masking, the VTR ops reads call these endpoints, which read ONLY
// the de-identified views as app_vtr_role (NO grant on raw / decrypt — VT-281). The orchestrator is
// the ONLY door to VTR data. Fail-CLOSED: any error → [] (the surface degrades to empty, never to
// raw). Multi-VTR (VT-377/mig-134): the views ARE assignment-scoped per-operator (admin tier sees
// all via the audited break-glass role) — the old "needs scoping first" precondition is closed.
// ---------------------------------------------------------------------------

const _VTR_READ_TIMEOUT_MS = 5000

export type VtrRow = Record<string, unknown>

async function fetchVtrRows(path: string, operatorId: string): Promise<VtrRow[]> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
  const secret = process.env.INTERNAL_API_SECRET ?? ''
  try {
    const jwt = await issueOperatorJwt(operatorId)
    const res = await fetch(`${base}${path}`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-Internal-Secret': secret,
        'X-Operator-Jwt': jwt,
      },
      body: JSON.stringify({ operator_id: operatorId }),
      signal: AbortSignal.timeout(_VTR_READ_TIMEOUT_MS),
    })
    if (!res.ok) {
      console.error(`fetchVtrRows ${path}: http_${res.status}; failing closed`)
      return []
    }
    const data = (await res.json()) as { rows?: VtrRow[] }
    return data.rows ?? []
  } catch (err) {
    console.error(`fetchVtrRows ${path}: failing closed`, err)
    return []
  }
}

/** VT-360: VTR escalations queue (de-identified, app_vtr_role + vtr_escalations). Fail-closed []. */
export function fetchVtrEscalations(operatorId: string): Promise<VtrRow[]> {
  return fetchVtrRows('/api/orchestrator/ops/vtr-escalations', operatorId)
}

/** VT-360: VTR monitoring board (de-identified, app_vtr_role + vtr_tenant_alerts). Fail-closed []. */
export function fetchVtrMonitoring(operatorId: string): Promise<VtrRow[]> {
  return fetchVtrRows('/api/orchestrator/ops/vtr-monitoring', operatorId)
}


// ---------------------------------------------------------------------------
// VT-361 — business verification (two-tier) proxy. team-web NEVER calls Sandbox directly; the
// orchestrator holds the creds + does the GSTIN lookup (fail-closed). Owner enters a GSTIN on the
// owner surface → lookup → gstin_verified ("yellow"). The "green" upgrade is the ops-only
// forwardVtrVerify (operator-JWT). No penny-drop (cut — Fazal two-tier ruling 2026-06-08).
// ---------------------------------------------------------------------------

export interface BusinessVerificationResult {
  ok: boolean
  status?: string // unverified | gstin_verified | vtr_verified
  reason?: string // vendor_down (retry) | invalid_gstin | attempt_cap
  name?: string | null
  raw?: Record<string, unknown>
}

export async function forwardBusinessVerification(
  tenantId: string,
  gstin: string,
): Promise<BusinessVerificationResult> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
  const secret = process.env.INTERNAL_API_SECRET ?? ''
  try {
    const res = await fetch(`${base}/api/business-verification`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'X-Internal-Secret': secret },
      body: JSON.stringify({ tenant_id: tenantId, gstin }),
      signal: AbortSignal.timeout(_FORWARD_TIMEOUT_MS),
    })
    if (!res.ok) return { ok: false, reason: `http_${res.status}` }
    const data = (await res.json()) as Record<string, unknown>
    return {
      ok: Boolean(data.ok),
      status: (data.status as string) ?? undefined,
      reason: (data.reason as string) ?? undefined,
      name: (data.name as string) ?? null,
      raw: data,
    }
  } catch (err) {
    const timedOut = err instanceof Error && err.name === 'TimeoutError'
    return { ok: false, reason: timedOut ? 'timeout' : 'error' }
  }
}

/** VT-361 — VTR "green" override (ops-only; operator-JWT). Audited server-side. */
export async function forwardVtrVerify(
  tenantId: string,
  operatorId: string,
  operatorJwt: string,
  basis: string,
): Promise<{ ok: boolean; status?: string; reason?: string }> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
  const secret = process.env.INTERNAL_API_SECRET ?? ''
  try {
    const res = await fetch(`${base}/api/orchestrator/ops/vtr-verify`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-Internal-Secret': secret,
        'X-Operator-Jwt': operatorJwt,
      },
      body: JSON.stringify({ tenant_id: tenantId, operator_id: operatorId, basis }),
      signal: AbortSignal.timeout(_FORWARD_TIMEOUT_MS),
    })
    if (!res.ok) return { ok: false, reason: `http_${res.status}` }
    const data = (await res.json()) as Record<string, unknown>
    return { ok: Boolean(data.ok), status: (data.status as string) ?? undefined }
  } catch (err) {
    const timedOut = err instanceof Error && err.name === 'TimeoutError'
    return { ok: false, reason: timedOut ? 'timeout' : 'error' }
  }
}


// ---------------------------------------------------------------------------
// VT-370 Gap-6 — VTR console (plan-editing + agent-correction) client.
//
// ALL seven fns ride the forwardVtrVerify template: X-Internal-Secret (transport) +
// X-Operator-Jwt (attribution) — NEVER the forwardRunControl shape (no JWT, body-trusted
// operator_id = forgeable attribution). Every JWT is minted with
// `{ ttlSec: OPERATOR_RESOLVE_TTL_SEC }` (5 min) — the bare issueOperatorJwt default is
// 7 DAYS (operator-jwt.ts) and is forbidden here; these tokens cross the orchestrator
// audit boundary (CL-390). `operator_id` is always passed through from the caller
// (server actions derive it from the session claim — never client input); the
// orchestrator re-verifies body operator_id == JWT claim + assignment, fail-closed.
//
// CL-390 logging: these fns log path + status ONLY. Never the patch, params, reason,
// violations, or any response body (an echoed body in Vercel logs is a CL-390 breach).
// ---------------------------------------------------------------------------

const _VTR_ACTION_TIMEOUT_MS = 10_000

interface VtrCall {
  /** HTTP status; 0 on network throw/timeout. */
  status: number
  body: Record<string, unknown>
  /** ok | http_<n> | timeout | error */
  reason: string
}

async function vtrCall(path: string, operatorId: string, payload: Record<string, unknown>): Promise<VtrCall> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
  const secret = process.env.INTERNAL_API_SECRET ?? ''
  try {
    // Short-lived by mandate — never the bare 7-day default.
    const jwt = await issueOperatorJwt(operatorId, { ttlSec: OPERATOR_RESOLVE_TTL_SEC })
    const res = await fetch(`${base}/api/orchestrator/ops/${path}`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-Internal-Secret': secret,
        'X-Operator-Jwt': jwt,
      },
      body: JSON.stringify({ operator_id: operatorId, ...payload }),
      signal: AbortSignal.timeout(_VTR_ACTION_TIMEOUT_MS),
    })
    let body: Record<string, unknown> = {}
    try {
      body = (await res.json()) as Record<string, unknown>
    } catch {
      body = {}
    }
    if (!res.ok) console.error(`vtr ${path}: http_${res.status}`) // CL-390: path + status only
    return { status: res.status, body, reason: res.ok ? 'ok' : `http_${res.status}` }
  } catch (err) {
    const timedOut = err instanceof Error && err.name === 'TimeoutError'
    console.error(`vtr ${path}: ${timedOut ? 'timeout' : 'error'}`) // CL-390: no err detail/body
    return { status: 0, body: {}, reason: timedOut ? 'timeout' : 'error' }
  }
}

/** FastAPI HTTPException `detail` → display strings (server scrubs PII before raising). */
function _detailStrings(body: Record<string, unknown>): string[] {
  const detail = body.detail
  if (typeof detail === 'string') return [detail]
  if (Array.isArray(detail)) return detail.map((d) => (typeof d === 'string' ? d : JSON.stringify(d)))
  if (detail != null) return [JSON.stringify(detail)]
  return []
}

/** One roadmap item as the vtr_business_plan view ships it (diff_from_prev values pre-stripped). */
export interface VtrRoadmapItem {
  item_id: string
  seq: number
  month: number
  objective: string
  why: string
  owning_agent: string
  owner_action_needed: boolean
  owner_action: string | null
  owner_action_hi: string | null
  status: string
  cited_facts?: string[]
  provenance?: Record<string, unknown>
}

export interface VtrPlan {
  tenant_id: string
  version: number
  summary_json: Record<string, unknown> | null
  roadmap_json: VtrRoadmapItem[]
  generated_by: string
  model_id: string | null
  delivered_parts: number
  delivered_at: string | null
  created_at: string | null
}

/** vtr_plan_history view row — metadata ONLY (no roadmap/summary content for prior versions). */
export interface VtrPlanHistoryEntry {
  tenant_id: string
  version: number
  generated_by: string
  model_id: string | null
  created_at: string | null
}

export interface VtrPlanResult {
  ok: boolean
  plan: VtrPlan | null
  history: VtrPlanHistoryEntry[]
  reason: string
}

/** Gap-6: latest plan (latest-version-only, diff-values-stripped view) + metadata history. */
export async function vtrPlan(operatorId: string, tenantId: string): Promise<VtrPlanResult> {
  const r = await vtrCall('vtr-plan', operatorId, { tenant_id: tenantId })
  if (r.status !== 200) return { ok: false, plan: null, history: [], reason: r.reason }
  return {
    ok: true,
    plan: (r.body.plan as VtrPlan | null) ?? null,
    history: (r.body.history as VtrPlanHistoryEntry[] | undefined) ?? [],
    reason: 'ok',
  }
}

export interface VtrPlanEditResult {
  ok: boolean
  newVersion: number | null
  /** ok | grounding_or_patch | forbidden | not_found | stale_version | http_<n> | timeout | error */
  reason: string
  /** Scrubbed grounding-violation strings (400 only). Render-only — never log (CL-390). */
  violations: string[]
}

/**
 * Gap-6: edit one roadmap item (EDITABLE_FIELDS patch). Carries `expected_prev_version`
 * (optimistic concurrency) — 409 means the plan moved underneath the loaded copy.
 */
export async function vtrPlanEdit(
  operatorId: string,
  tenantId: string,
  itemId: string,
  patch: Record<string, unknown>,
  expectedPrevVersion: number,
): Promise<VtrPlanEditResult> {
  const r = await vtrCall('vtr-plan-edit', operatorId, {
    tenant_id: tenantId,
    item_id: itemId,
    patch,
    expected_prev_version: expectedPrevVersion,
  })
  if (r.status === 200) {
    return {
      ok: true,
      newVersion: typeof r.body.new_version === 'number' ? r.body.new_version : null,
      reason: 'ok',
      violations: [],
    }
  }
  if (r.status === 400) {
    return { ok: false, newVersion: null, reason: 'grounding_or_patch', violations: _detailStrings(r.body) }
  }
  if (r.status === 403) return { ok: false, newVersion: null, reason: 'forbidden', violations: [] }
  if (r.status === 404) return { ok: false, newVersion: null, reason: 'not_found', violations: [] }
  if (r.status === 409) return { ok: false, newVersion: null, reason: 'stale_version', violations: [] }
  return { ok: false, newVersion: null, reason: r.reason, violations: [] }
}

/** vtr_agent_autonomy view row. NO revoke_reason (excluded by construction — free text). */
export interface VtrAgentAutonomy {
  tenant_id: string
  tenant_name: string | null
  agent: string
  level: string
  clean_approval_streak: number
  lifetime_approvals: number
  lifetime_rejections: number
  frozen: boolean
  last_regression_at: string | null
  last_regression_kind: string | null
  l3_granted_at: string | null
  l3_revoked_at: string | null
  updated_at: string | null
}

/** Gap-6: per-agent autonomy state. Fail-closed [] (a missing agent row = L2/0/unfrozen default). */
export async function vtrAgentState(
  operatorId: string,
  tenantId: string,
): Promise<{ ok: boolean; agents: VtrAgentAutonomy[]; reason: string }> {
  const r = await vtrCall('vtr-agent-state', operatorId, { tenant_id: tenantId })
  if (r.status !== 200) return { ok: false, agents: [], reason: r.reason }
  return { ok: true, agents: (r.body.agents as VtrAgentAutonomy[] | undefined) ?? [], reason: 'ok' }
}

/**
 * vtr_tenant_profile view row (VT-405 Part A) — signup fields + auto-discovered draft + keys-only
 * confirmation status. Non-PII: WhatsApp is masked to last-4 AT the view; the confirmed canonical
 * profile is keys-only (`confirmed_fields`); draft attributes/provenance carry public discovery values.
 */
export interface VtrTenantProfile {
  tenant_id: string
  business_name: string | null
  phase: string | null
  plan_tier: string | null
  business_type: string | null
  locality: string | null
  city_tier: string | null
  language_preference: string | null
  preferred_language: string | null
  signed_up_at: string | null
  trial_started_at: string | null
  phase_entered_at: string | null
  owner_name: string | null
  whatsapp_last4: string | null
  draft_attributes: Record<string, unknown> | null
  draft_provenance: Record<string, { source?: string; fetched_at?: string }> | null
  draft_created_at: string | null
  draft_updated_at: string | null
  onboarding_status: string | null
  onboarding_queue_len: number
  confirmed_fields: string[] | null
}

export async function vtrTenantProfile(
  operatorId: string,
  tenantId: string,
): Promise<{ ok: boolean; profile: VtrTenantProfile | null; reason: string }> {
  const r = await vtrCall('vtr-tenant-profile', operatorId, { tenant_id: tenantId })
  if (r.status !== 200) return { ok: false, profile: null, reason: r.reason }
  return { ok: true, profile: (r.body.profile as VtrTenantProfile | null) ?? null, reason: 'ok' }
}

/** vtr_draft_batches view row — AGGREGATES ONLY (no params/owner_feedback/customer_id by view). */
export interface VtrDraftBatch {
  batch_id: string
  tenant_id: string
  tenant_name: string | null
  agent: string
  status: string
  edit_cycles: number
  created_at: string | null
  updated_at: string | null
  draft_count: number
  pending_count: number
  sent_count: number
  skipped_count: number
  halted_count: number
  template_names: (string | null)[]
}

/** Gap-6: draft batches (counts + template-name enums only). Fail-closed []. */
export async function vtrDraftBatches(
  operatorId: string,
  tenantId: string,
  limit = 100,
): Promise<{ ok: boolean; rows: VtrDraftBatch[]; count: number; reason: string }> {
  const r = await vtrCall('vtr-draft-batches', operatorId, { tenant_id: tenantId, limit })
  if (r.status !== 200) return { ok: false, rows: [], count: 0, reason: r.reason }
  const rows = (r.body.rows as VtrDraftBatch[] | undefined) ?? []
  return { ok: true, rows, count: typeof r.body.count === 'number' ? r.body.count : rows.length, reason: 'ok' }
}

export type VtrOverrideAction = 'freeze' | 'unfreeze' | 'demote' | 'revoke_l3'

export interface VtrOverrideResult {
  ok: boolean
  state: { level: string; frozen: boolean; streak: number } | null
  batchesCancelled: number
  /** ok | forbidden | http_<n> | timeout | error */
  reason: string
}

/** Gap-6: freeze/unfreeze/demote/revoke_l3 one agent. Reason is scrubbed + clamped server-side. */
export async function vtrAutonomyOverride(
  operatorId: string,
  tenantId: string,
  agent: string,
  action: VtrOverrideAction,
  reason = '',
): Promise<VtrOverrideResult> {
  const r = await vtrCall('vtr-autonomy-override', operatorId, {
    tenant_id: tenantId,
    agent,
    action,
    reason,
  })
  if (r.status !== 200) {
    return { ok: false, state: null, batchesCancelled: 0, reason: r.status === 403 ? 'forbidden' : r.reason }
  }
  const state = (r.body.state as { level: string; frozen: boolean; streak: number } | undefined) ?? null
  return {
    ok: true,
    state,
    batchesCancelled: typeof r.body.batches_cancelled === 'number' ? r.body.batches_cancelled : 0,
    reason: 'ok',
  }
}

export interface VtrBatchCancelResult {
  ok: boolean
  tenantId: string | null
  draftsHalted: number
  /** ok | forbidden | not_found | http_<n> | timeout | error */
  reason: string
}

/**
 * Gap-6: cancel ONE batch (the scalpel). NO tenant_id crosses the wire — the orchestrator
 * derives it from the batch row server-side (VT-293/294 IDOR discipline; missing → 404).
 */
export async function vtrBatchCancel(
  operatorId: string,
  batchId: string,
  reason = '',
): Promise<VtrBatchCancelResult> {
  const r = await vtrCall('vtr-batch-cancel', operatorId, { batch_id: batchId, reason })
  if (r.status !== 200) {
    const mapped = r.status === 403 ? 'forbidden' : r.status === 404 ? 'not_found' : r.reason
    return { ok: false, tenantId: null, draftsHalted: 0, reason: mapped }
  }
  return {
    ok: true,
    tenantId: (r.body.tenant_id as string | undefined) ?? null,
    draftsHalted: typeof r.body.drafts_halted === 'number' ? r.body.drafts_halted : 0,
    reason: 'ok',
  }
}

/** Per-draft row from the EXCEPTION-TIER drill-in (params visible — Fazal-only, audited). */
export interface VtrBatchDraft {
  template_name: string
  params: Record<string, unknown> | null
  status: string
  skip_reason: string | null
}

/**
 * Gap-6 exception tier (Fazal=VTR#1 only): per-draft template_name + params for one batch.
 * The orchestrator audits the reveal in-txn BEFORE the read. 403 = not exception tier —
 * callers render that gracefully (the button shows for everyone; the gate is server-side).
 * CL-390: NEVER log the returned drafts.
 */
export async function vtrBatchDrafts(
  operatorId: string,
  batchId: string,
): Promise<{ ok: boolean; drafts: VtrBatchDraft[]; reason: string }> {
  const r = await vtrCall('vtr-batch-drafts', operatorId, { batch_id: batchId })
  if (r.status !== 200) {
    const mapped = r.status === 403 ? 'forbidden' : r.status === 404 ? 'not_found' : r.reason
    return { ok: false, drafts: [], reason: mapped }
  }
  return { ok: true, drafts: (r.body.drafts as VtrBatchDraft[] | undefined) ?? [], reason: 'ok' }
}


// ---------------------------------------------------------------------------
// VT-375 (Phase B) — VTR run-control READ surface (programs projection + step
// timeline). READ-ONLY: GET endpoints on the Gap-6 stack — X-Internal-Secret
// (transport) + a SHORT-LIVED X-Operator-Jwt (attribution). GET carries NO body:
// the orchestrator feeds the JWT claim id itself to the assignment-gate equality
// leg (it never reads an operator_id from a GET body), so these fns mint the
// short-lived token and send NOTHING in a body. Fail-CLOSED (empty projection /
// empty timeline) on any non-2xx or throw — the canvas degrades to empty, never
// to raw. CL-390: log path + status ONLY — never the projection/timeline body.
//
// The write leg (pause / override / rerun) ships below this read surface (the
// VT-376 mutation fns — see the VT-376 section further down this file).
// ---------------------------------------------------------------------------

const _VTR_RC_READ_TIMEOUT_MS = 10_000

/** GET on the run-control read stack — short-lived JWT, no body. status 0 on throw/timeout. */
async function vtrRcGet(path: string, operatorId: string): Promise<VtrCall> {
  const base = process.env.TEAM_ORCHESTRATOR_URL ?? _ORCHESTRATOR_DEFAULT
  const secret = process.env.INTERNAL_API_SECRET ?? ''
  try {
    const jwt = await issueOperatorJwt(operatorId, { ttlSec: OPERATOR_RESOLVE_TTL_SEC })
    const res = await fetch(`${base}/api/orchestrator/ops/run-control/${path}`, {
      method: 'GET',
      headers: {
        'X-Internal-Secret': secret,
        'X-Operator-Jwt': jwt,
      },
      signal: AbortSignal.timeout(_VTR_RC_READ_TIMEOUT_MS),
    })
    let body: Record<string, unknown> = {}
    try {
      body = (await res.json()) as Record<string, unknown>
    } catch {
      body = {}
    }
    if (!res.ok) console.error(`vtr-rc ${path}: http_${res.status}`) // CL-390: path + status only
    return { status: res.status, body, reason: res.ok ? 'ok' : `http_${res.status}` }
  } catch (err) {
    const timedOut = err instanceof Error && err.name === 'TimeoutError'
    console.error(`vtr-rc ${path}: ${timedOut ? 'timeout' : 'error'}`) // CL-390: no err detail/body
    return { status: 0, body: {}, reason: timedOut ? 'timeout' : 'error' }
  }
}

/** One terminal/running run as the programs projection ships it (past + running share fields). */
export interface VtrProgramRun {
  run_id: string
  run_type: string | null
  status: string
  started_at: string | null
  ended_at: string | null
  rerun_of_run_id: string | null
  rerun_from_step: string | null
  step_count: number | null
  /** running rows only — an active workflow_controls hold covers this run's kind. */
  active_hold?: boolean
}

/** One COMPUTED upcoming-7d forecast item (no new state — trial sweep / agent dispatch / roadmap). */
export interface VtrUpcomingItem {
  kind: string
  due_at: string | null
  label: string
  source: string
}

/** One active pause hold (vtr_workflow_controls — structural fields only; NEVER reason). */
export interface VtrHold {
  // /programs sends exactly {workflow_kind, set_at} (deliberately minimal — no reason text);
  // /timeline's active_controls additionally carry tenant_id/released_at from the view.
  tenant_id?: string
  workflow_kind: string
  set_at: string | null
  released_at?: string | null
}

export interface VtrProgramsResult {
  ok: boolean
  past: VtrProgramRun[]
  running: VtrProgramRun[]
  upcoming7d: VtrUpcomingItem[]
  holds: VtrHold[]
  /** true when the workflow_controls read degraded (fail-open) — drives the unverifiable banner. */
  degraded: boolean
  reason: string
}

/**
 * VT-375: per-tenant programs projection (past / running / upcoming-7d + active holds).
 * Read-only. Fail-CLOSED on any non-2xx / throw → empty groups, degraded=true so the
 * canvas surfaces the pause-state-unverifiable copy rather than implying "not paused".
 */
export async function vtrPrograms(
  operatorId: string,
  tenantId: string,
): Promise<VtrProgramsResult> {
  const r = await vtrRcGet(`programs/${tenantId}`, operatorId)
  if (r.status !== 200) {
    return { ok: false, past: [], running: [], upcoming7d: [], holds: [], degraded: true, reason: r.reason }
  }
  return {
    ok: true,
    past: (r.body.past as VtrProgramRun[] | undefined) ?? [],
    running: (r.body.running as VtrProgramRun[] | undefined) ?? [],
    upcoming7d: (r.body.upcoming_7d as VtrUpcomingItem[] | undefined) ?? [],
    holds: (r.body.holds as VtrHold[] | undefined) ?? [],
    degraded: Boolean(r.body.degraded),
    reason: 'ok',
  }
}

/** One step row as vtr_step_timeline ships it — keys-only envelopes by construction (plan §6). */
export interface VtrTimelineStep {
  run_id: string
  run_type: string | null
  run_status: string | null
  run_started_at: string | null
  run_ended_at: string | null
  rerun_of_run_id: string | null
  rerun_from_step: string | null
  step_id: string | null
  step_seq: number | null
  step_kind: string | null
  step_name: string | null
  step_status: string | null
  /**
   * Control axis (orchestrator-authoritative): 'controllable' = pause/override/rerun can act on
   * this step; 'observed' = timeline display only, not controllable in this panel. Drives the
   * "Observed — not controllable" badge. Absent/unknown ⇒ treat as observed (fail-safe: never
   * imply a step is controllable when the server didn't say so).
   */
  tier: 'controllable' | 'observed' | null
  /**
   * VT-376 (B1 /timeline annotation): the KEY NAMES the override form may pin for this step —
   * the registry StepEntry.allowed_keys (config/ID-class only, I7-safe to show). Empty array for
   * observed/uncontrollable steps. NEVER the VALUES (those are de-identified by construction); the
   * override dialog renders one field per name and warns the operator is editing blind.
   */
  allowed_keys?: string[]
  started_at: string | null
  ended_at: string | null
  duration_ms: number | null
  override_id: string | null
  paused_ms: number | null
  /** keys-only (array of key names) for non-audited kinds; an object for the audited name-free set. */
  input_envelope: unknown
  output_envelope: unknown
}

export interface VtrRunTimelineResult {
  ok: boolean
  runId: string | null
  tenantId: string | null
  steps: VtrTimelineStep[]
  /** active vtr_workflow_controls holds riding along so the panel never shows a false "not paused". */
  activeControls: VtrHold[]
  /**
   * VT-376 (B1 run-level annotation): true iff this run's workflow_kind is in RERUNNABLE — drives
   * whether the rerun button shows at all. Absent ⇒ false (fail-safe: never offer rerun the server
   * didn't bless).
   */
  rerunnable: boolean
  /**
   * VT-376: per-kind WHY-copy when the run is NOT rerunnable
   * ('message-dedup semantics' | 'duplicate-nudge risk' | 'kg-duplication'). null when rerunnable.
   * Rendered verbatim next to the (absent) button so the operator sees the reason, not silence.
   */
  forbiddenReason: string | null
  /**
   * VT-376 PRE-FLIGHT (a3): true iff the tenant has an OPEN owner approval right now — a rerun
   * would refuse (server 409/422 is the authority). The confirm dialog re-fetches this immediately
   * pre-POST and, when true, warns + disables submit. Absent ⇒ false (the server gate still
   * decides; this is UI sugar). NEVER the approval's contents — a boolean only.
   */
  openApproval: boolean
  reason: string
}

/**
 * VT-375: the step timeline for ONE run (the Phase-A GET surface). Read-only.
 * Tenant is DERIVED server-side from the run row (VT-293/294) — no tenant crosses the wire.
 * Fail-CLOSED on non-2xx / throw → empty steps.
 */
export async function vtrRunTimeline(
  operatorId: string,
  runId: string,
): Promise<VtrRunTimelineResult> {
  const r = await vtrRcGet(`timeline/${runId}`, operatorId)
  if (r.status !== 200) {
    return {
      ok: false,
      runId: null,
      tenantId: null,
      steps: [],
      activeControls: [],
      rerunnable: false,
      forbiddenReason: null,
      openApproval: false,
      reason: r.reason,
    }
  }
  // VT-376 run-level annotations (B1) — all fail-safe-defaulted: a server that omits them is
  // treated as "not rerunnable / no open-approval signal", never as "rerun is fine".
  return {
    ok: true,
    runId: (r.body.run_id as string | undefined) ?? null,
    tenantId: (r.body.tenant_id as string | undefined) ?? null,
    steps: (r.body.steps as VtrTimelineStep[] | undefined) ?? [],
    activeControls: (r.body.active_controls as VtrHold[] | undefined) ?? [],
    rerunnable: Boolean(r.body.rerunnable),
    forbiddenReason:
      typeof r.body.forbidden_reason === 'string' ? r.body.forbidden_reason : null,
    openApproval: Boolean(r.body.open_approval),
    reason: 'ok',
  }
}


// ---------------------------------------------------------------------------
// VT-376 (Phase C) — VTR run-control MUTATION surface (pause/release/override/
// cancel-override/rerun). POST on the Gap-6 vtrCall idiom (X-Internal-Secret +
// SHORT-LIVED X-Operator-Jwt) — the SAME endpoints the orchestrator built in
// Phase A; the panel invents NO new mutation paths. Every fn maps the pinned
// status codes (401/403/404/409/422/503) to a TYPED reason; the orchestrator
// re-derives tenant + re-checks the assignment gate + audits BEFORE the mutation
// (team-web auth is fail-open at the enforcement leg, server is authoritative).
//
// CL-390: vtrCall already logs path + status ONLY — these fns NEVER log the
// reason text, pins, override ids, or any response body. NO tenant_id crosses the
// wire for the ROW-targeted actions (cancel-override / rerun) — the orchestrator
// derives it from the row (VT-293/294 IDOR discipline).
// ---------------------------------------------------------------------------

/** Map a vtrCall result to the run-control typed reason vocabulary (shared across the 5 fns). */
function _rcReason(status: number, fallback: string): string {
  switch (status) {
    case 401:
      return 'unauthorized'
    case 403:
      return 'forbidden'
    case 404:
      return 'not_found'
    case 409:
      return 'conflict'
    case 422:
      return 'unprocessable'
    case 503:
      return 'registry_unavailable'
    default:
      return fallback
  }
}

export interface RcPauseResult {
  ok: boolean
  controlId: string | null
  /** ok | unauthorized | forbidden | conflict (already paused) | http_<n> | timeout | error */
  reason: string
}

/**
 * VT-376: set the (tenant, workflow_kind) hold. 409 = already paused. The server F9 read-back
 * means a 200 here is a hold the executor can actually see (a pause it can't see is a 500, not
 * a false success). ``reason`` free text is redacted at write — the UI notes that.
 */
export async function vtrRcPause(
  operatorId: string,
  tenantId: string,
  workflowKind: string,
  reason = '',
): Promise<RcPauseResult> {
  const r = await vtrCall('run-control/pause', operatorId, {
    tenant_id: tenantId,
    workflow_kind: workflowKind,
    reason,
  })
  if (r.status !== 200) {
    return { ok: false, controlId: null, reason: _rcReason(r.status, r.reason) }
  }
  return { ok: true, controlId: (r.body.control_id as string | undefined) ?? null, reason: 'ok' }
}

export interface RcReleaseResult {
  ok: boolean
  controlId: string | null
  /** ok | unauthorized | forbidden | not_found (no active pause) | http_<n> | timeout | error */
  reason: string
}

/** VT-376: release the active (tenant, workflow_kind) hold. 404 = no active pause. */
export async function vtrRcRelease(
  operatorId: string,
  tenantId: string,
  workflowKind: string,
): Promise<RcReleaseResult> {
  const r = await vtrCall('run-control/release', operatorId, {
    tenant_id: tenantId,
    workflow_kind: workflowKind,
  })
  if (r.status !== 200) {
    return { ok: false, controlId: null, reason: _rcReason(r.status, r.reason) }
  }
  return { ok: true, controlId: (r.body.control_id as string | undefined) ?? null, reason: 'ok' }
}

export interface RcOverrideResult {
  ok: boolean
  overrideId: string | null
  expiresAt: string | null
  /**
   * ok | unauthorized | forbidden | not_found | unprocessable (422 — bad keys / non-controllable
   * step / pause-only boundary / gate module / next-run-needs-expiry) | registry_unavailable (503)
   * | http_<n> | timeout | error
   */
  reason: string
  /** Scrubbed 422/4xx detail strings — render-only, NEVER log (CL-390). */
  detail: string[]
}

/**
 * VT-376: pre-register a one-shot step pin. ``workflowId`` set = row-targeted (tenant derived
 * from the run); NULL = next-run, tenant-scoped, ``expiresAt`` REQUIRED (UI defaults 7d).
 * The server 422 is the authority on allowed-keys / pure_return / pause-only / gate-module /
 * next-run-expiry — the form only renders the step's allowed_keys (key NAMES; values blind).
 */
export async function vtrRcOverride(
  operatorId: string,
  args: {
    tenantId: string
    workflowKind: string
    stepName: string
    workflowId?: string | null
    pinnedInput?: Record<string, unknown> | null
    pinnedOutput?: Record<string, unknown> | null
    reason?: string
    expiresAt?: string | null
  },
): Promise<RcOverrideResult> {
  const r = await vtrCall('run-control/override', operatorId, {
    tenant_id: args.tenantId,
    workflow_kind: args.workflowKind,
    step_name: args.stepName,
    workflow_id: args.workflowId ?? null,
    pinned_input: args.pinnedInput ?? null,
    pinned_output: args.pinnedOutput ?? null,
    reason: args.reason ?? '',
    expires_at: args.expiresAt ?? null,
  })
  if (r.status !== 200) {
    return {
      ok: false,
      overrideId: null,
      expiresAt: null,
      reason: _rcReason(r.status, r.reason),
      detail: _detailStrings(r.body),
    }
  }
  return {
    ok: true,
    overrideId: (r.body.override_id as string | undefined) ?? null,
    expiresAt: (r.body.expires_at as string | undefined) ?? null,
    reason: 'ok',
    detail: [],
  }
}

export interface RcCancelOverrideResult {
  ok: boolean
  /** ok | unauthorized | forbidden | not_found | conflict (consumed/cancelled) | http_<n> | … */
  reason: string
}

/**
 * VT-376: cancel ONE unconsumed override. NO tenant_id crosses the wire — the orchestrator
 * derives it from the override row (VT-293/294). 409 = already consumed/cancelled (or lost the
 * race with a consuming run).
 */
export async function vtrRcCancelOverride(
  operatorId: string,
  overrideId: string,
): Promise<RcCancelOverrideResult> {
  const r = await vtrCall('run-control/cancel-override', operatorId, { override_id: overrideId })
  if (r.status !== 200) return { ok: false, reason: _rcReason(r.status, r.reason) }
  return { ok: true, reason: 'ok' }
}

export interface RcRerunResult {
  ok: boolean
  newRunId: string | null
  /**
   * The C1/Option-A close outcome — 'completed' | 'escalated_overlap'. The server returns 200
   * for BOTH (the rerun DID run; an overlap is disclosed, never rolled back). null on any non-200.
   */
  outcome: 'completed' | 'escalated_overlap' | null
  /**
   * ok | unauthorized | forbidden | not_found | conflict (paused) | unprocessable (422 — kind not
   * rerunnable / open approval / unknown step / non-object pin) | http_<n> | timeout | error
   */
  reason: string
  /** Scrubbed refusal detail — render-only, NEVER log (CL-390). */
  detail: string[]
}

/**
 * VT-376: app-level re-dispatch from a step (re-dispatch, NOT time-travel). NO tenant_id crosses
 * the wire — derived from the source run (VT-293/294). Outputs RE-ENTER owner approval (I2). An
 * owner approval that armed mid-flight closes the run 'escalated' with outcome
 * 'escalated_overlap' (still HTTP 200 — the panel surfaces the C1-A disclosure). The double-click
 * hazard is serialized server-side by the rerun-slot advisory lock (B1); the second concurrent
 * POST refuses cleanly (no two lineage rows for one source).
 */
export async function vtrRcRerun(
  operatorId: string,
  sourceRunId: string,
  fromStep: string,
  overrides: Record<string, unknown>[] = [],
): Promise<RcRerunResult> {
  const r = await vtrCall('run-control/rerun', operatorId, {
    source_run_id: sourceRunId,
    from_step: fromStep,
    overrides,
  })
  if (r.status !== 200) {
    return {
      ok: false,
      newRunId: null,
      outcome: null,
      reason: _rcReason(r.status, r.reason),
      detail: _detailStrings(r.body),
    }
  }
  const outcome = r.body.outcome === 'escalated_overlap' ? 'escalated_overlap' : 'completed'
  return {
    ok: true,
    newRunId: (r.body.new_run_id as string | undefined) ?? null,
    outcome,
    reason: 'ok',
    detail: [],
  }
}
