/**
 * VT-192 (absorbed into VT-123) — operator-resolve route.
 *
 * Flow:
 *   1. `requireFazal()` — Phase-1 auth gate; 403 on miss.
 *   2. Issue short-lived operator-claim JWT.
 *   3. POST to orchestrator's `ops/resolve-phone` endpoint (Q3 Option A
 *      decrypt-proxy locked per Cowork plan-review — encryption key
 *      stays in the orchestrator Python process; team-web is the
 *      Phase-1 caller surface).
 *   4. Return `{phone_e164}` or `{error}`.
 *
 * Per VT-188 stored function: every call writes an audit row inside
 * the function's transaction. Atomicity at the DB layer means there is
 * no "resolved-without-audit" failure mode.
 */

import { NextResponse } from 'next/server'

import { requireFazal, UnauthorizedError } from '@/lib/auth/require-fazal'
import { issueOperatorJwt, OPERATOR_RESOLVE_TTL_SEC } from '@/lib/auth/operator-jwt'

export const runtime = 'nodejs'

const ORCHESTRATOR_BASE =
  process.env.TEAM_ORCHESTRATOR_URL ?? 'http://localhost:8001'
const INTERNAL_SECRET = process.env.INTERNAL_API_SECRET ?? ''

interface ResolveBody {
  phone_token: string
  step_id?: string
}

export async function POST(req: Request) {
  let fazalUuid: string
  try {
    ;({ fazalUuid } = await requireFazal())
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      return NextResponse.json({ error: err.message }, { status: 403 })
    }
    throw err
  }

  let body: ResolveBody
  try {
    body = (await req.json()) as ResolveBody
  } catch {
    return NextResponse.json({ error: 'invalid JSON' }, { status: 400 })
  }
  if (!body.phone_token || typeof body.phone_token !== 'string') {
    return NextResponse.json({ error: 'phone_token required' }, { status: 400 })
  }

  // VT-236: explicit short TTL — resolve-phone crosses orchestrator
  // audit boundary; CL-390 keeps this token short-lived.
  const jwt = await issueOperatorJwt(fazalUuid, { ttlSec: OPERATOR_RESOLVE_TTL_SEC })
  const upstream = await fetch(`${ORCHESTRATOR_BASE}/api/orchestrator/ops/resolve-phone`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'X-Internal-Secret': INTERNAL_SECRET,
      'X-Operator-Jwt': jwt,
    },
    body: JSON.stringify({
      phone_token: body.phone_token,
      operator_id: fazalUuid,
    }),
  })
  if (!upstream.ok) {
    const text = await upstream.text()
    return NextResponse.json(
      { error: `orchestrator returned ${upstream.status}: ${text}` },
      { status: upstream.status },
    )
  }
  const data = (await upstream.json()) as { phone_e164: string | null }
  return NextResponse.json(data)
}
