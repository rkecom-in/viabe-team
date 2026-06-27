/**
 * VT-406 (Part B) — proxy POST for the signup entity-match GSTIN confirm.
 *
 * The browser POSTs {gstin} (the picked candidate's registry id); this route forwards to the
 * orchestrator's internal-secret-gated /api/orchestrator/onboard/entity-confirm via the
 * server-side orchestrator-client (INTERNAL_API_SECRET never reaches the browser). The orchestrator
 * does the Sandbox round-trip; team-web NEVER calls Sandbox directly.
 *
 * tenant_id is '' here — the entity-match wizard runs BEFORE the tenant exists (VT-408
 * verify-then-create ordering). The Sandbox verify still returns a status; Part A's anchor-persist /
 * discovery-seed are best-effort no-ops without a real tenant. The verified entity is carried into
 * the create payload so the orchestrator anchors it at tenant-create time.
 *
 * Fail-CLOSED: a vendor/transport failure → {ok:false, reason} — NEVER a faked verified result.
 * CL-390: never log the gstin / name / any body (business identity).
 */
import { NextResponse } from 'next/server'

import { confirmEntity } from '@/lib/orchestrator-client'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

export async function POST(request: Request): Promise<Response> {
  const body = (await request.json().catch(() => null)) as Record<string, unknown> | null
  const gstin = body && typeof body.gstin === 'string' ? body.gstin : ''
  const businessName = body && typeof body.business_name === 'string' ? body.business_name : ''
  if (!gstin.trim()) {
    return NextResponse.json({ ok: false, reason: 'invalid_gstin_format', status: 'unverified' }, { status: 400 })
  }

  // tenant_id '' — pre-create (VT-408 ordering). The verify status round-trips; the verified entity
  // is carried client-side into the create payload, never anchored to a tenant that doesn't exist.
  // business_name threads through so the orchestrator enforces the name-match at verify (VT-#10).
  const result = await confirmEntity('', gstin, businessName)
  return NextResponse.json({
    ok: result.ok,
    status: result.status ?? 'unverified',
    reason: result.reason,
    name: result.name ?? null,
  })
}
