/**
 * VT-406 (Part B) — entity-match flow logic: candidate fetch, confirm sequence, the
 * verified/reject/retry classifier, the create-account gate, and the confirmable predicate.
 *
 * The load-bearing invariants pinned here:
 *   - "verified" is reachable ONLY through a confirm with status === 'gstin_verified' — a web/GBP
 *     candidate is never classified verified;
 *   - "none of these" AND an invalid_gstin confirm both collapse to `reject` with NO reason carried
 *     (no inactive-vs-not-found enumeration oracle);
 *   - vendor_down (and any transport failure) is `retry`, NOT a reject;
 *   - the create-account gate is false until a verified entity with a gstin is held;
 *   - the proxy fetches fail closed (empty candidates / unverified) on non-2xx / throw.
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { describe, expect, it, vi } from 'vitest'

import {
  canCreateAccount,
  classifyConfirm,
  confirmCandidate,
  fetchCandidates,
  isConfirmable,
  isValidGstinFormat,
  type EntityCandidate,
  type EntityConfirmResult,
} from '@/lib/entity-match'

function resp(status: number, body: unknown = {}): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response
}

const WEB_CANDIDATE: EntityCandidate = {
  trade_name: 'Sundaram Book Store',
  source: 'web',
  candidate_gstin: '29ABCDE1234F1Z5',
  legal_name: 'Sundaram Multi Pap Limited',
  detail: 'MG Road, Bengaluru · Bookstore',
}
const GBP_CANDIDATE: EntityCandidate = {
  trade_name: 'Sundaram Book Store',
  source: 'gbp',
  candidate_gstin: null,
  legal_name: null,
  detail: 'Jayanagar · Book shop',
}

describe('VT-406 fetchCandidates', () => {
  it('200 → candidates, posts to the proxy route with business_name + city', async () => {
    const f = vi.fn().mockResolvedValue(resp(200, { candidates: [WEB_CANDIDATE, GBP_CANDIDATE] }))
    const r = await fetchCandidates('Sundaram Book Store', 'Bengaluru', f)
    expect(r.ok).toBe(true)
    expect(r.candidates).toHaveLength(2)
    const [url, init] = f.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/team/onboard/entity-candidates')
    expect(JSON.parse(init.body as string)).toEqual({
      business_name: 'Sundaram Book Store',
      city: 'Bengaluru',
    })
  })

  it('fails CLOSED to an empty list on non-2xx (never stalls signup)', async () => {
    const f = vi.fn().mockResolvedValue(resp(502))
    expect(await fetchCandidates('X', 'Y', f)).toEqual({
      ok: false,
      candidates: [],
      reason: 'http_502',
    })
  })

  it('fails CLOSED to an empty list on throw', async () => {
    const f = vi.fn().mockRejectedValue(new Error('network'))
    expect(await fetchCandidates('X', 'Y', f)).toEqual({ ok: false, candidates: [], reason: 'error' })
  })
})

describe('VT-406 confirmCandidate + classifyConfirm', () => {
  it('candidates render + pick → confirm called with the picked gstin', async () => {
    const f = vi.fn().mockResolvedValue(resp(200, { ok: true, status: 'gstin_verified', name: 'SUNDARAM MULTI PAP LIMITED' }))
    const envelope = await confirmCandidate('29ABCDE1234F1Z5', f)
    const [url, init] = f.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/team/onboard/entity-confirm')
    expect(JSON.parse(init.body as string)).toEqual({ gstin: '29ABCDE1234F1Z5' })
    expect(envelope.status).toBe('gstin_verified')
  })

  it('"verified" ONLY after a gstin_verified confirm — uses the authoritative registry name', () => {
    const envelope: EntityConfirmResult = {
      ok: true,
      status: 'gstin_verified',
      name: 'SUNDARAM MULTI PAP LIMITED',
    }
    expect(classifyConfirm(envelope, '29ABCDE1234F1Z5')).toEqual({
      kind: 'verified',
      gstin: '29ABCDE1234F1Z5',
      name: 'SUNDARAM MULTI PAP LIMITED',
    })
  })

  it('a non-verified status (even ok:true) is NEVER verified → reject', () => {
    // Defence: an "ok" response that is not gstin_verified must not pass as verified.
    expect(classifyConfirm({ ok: true, status: 'unverified' }, 'G').kind).toBe('reject')
  })

  it('invalid_gstin → reject, with NO reason carried (no enumeration oracle)', () => {
    const out = classifyConfirm({ ok: false, status: 'unverified', reason: 'invalid_gstin' }, 'G')
    expect(out).toEqual({ kind: 'reject' })
    // The reject outcome leaks NO inactive-vs-not-found distinction.
    expect(Object.keys(out)).toEqual(['kind'])
  })

  it('invalid_gstin_format → reject (same generic terminus)', () => {
    expect(classifyConfirm({ ok: false, reason: 'invalid_gstin_format' }, 'G')).toEqual({ kind: 'reject' })
  })

  it('vendor_down → retry, NOT reject', () => {
    expect(classifyConfirm({ ok: false, reason: 'vendor_down' }, 'G')).toEqual({ kind: 'retry' })
  })

  it('http_5xx / timeout / transport error → retry, NOT reject', () => {
    expect(classifyConfirm({ ok: false, reason: 'http_503' }, 'G').kind).toBe('retry')
    expect(classifyConfirm({ ok: false, reason: 'timeout' }, 'G').kind).toBe('retry')
    expect(classifyConfirm({ ok: false, reason: 'error' }, 'G').kind).toBe('retry')
  })

  it('confirm proxy fails CLOSED on non-2xx (→ classified retry, never a false verified)', async () => {
    const f = vi.fn().mockResolvedValue(resp(500))
    const envelope = await confirmCandidate('G', f)
    expect(envelope.ok).toBe(false)
    expect(classifyConfirm(envelope, 'G').kind).toBe('retry')
  })

  it('confirm proxy fails CLOSED on throw (→ retry)', async () => {
    const f = vi.fn().mockRejectedValue(new Error('network'))
    const envelope = await confirmCandidate('G', f)
    expect(envelope).toEqual({ ok: false, reason: 'error' })
    expect(classifyConfirm(envelope, 'G').kind).toBe('retry')
  })
})

describe('VT-406 create-account gate (canCreateAccount)', () => {
  it('false until a verified entity with a gstin is held', () => {
    expect(canCreateAccount(null)).toBe(false)
    expect(canCreateAccount({ gstin: '', name: 'X' })).toBe(false)
  })
  it('true only with a verified gstin', () => {
    expect(canCreateAccount({ gstin: '29ABCDE1234F1Z5', name: 'X' })).toBe(true)
  })
})

describe('VT-406 isConfirmable (provenance — a GBP candidate cannot be verified)', () => {
  it('a web candidate WITH a gstin is confirmable', () => {
    expect(isConfirmable(WEB_CANDIDATE)).toBe(true)
  })
  it('a GBP candidate with NO gstin is NOT confirmable (verify needs a registry id)', () => {
    expect(isConfirmable(GBP_CANDIDATE)).toBe(false)
  })
})

describe('VT-406 provenance — the wizard never badges a candidate as verified', () => {
  const src = readFileSync(
    fileURLToPath(new URL('../../app/(marketing)/team/signup/entity-match-step.tsx', import.meta.url)),
    'utf-8',
  )

  it('the verified chip is rendered ONLY in the verified-step branch (data-entity-step="verified")', () => {
    // The verified chip copy must NOT appear inside the picking branch. Assert the verified-chip
    // message is reached only via verified_heading (the verified branch), and the picking branch
    // tags candidates with the found chip.
    expect(src).toContain('data-entity-step="verified"')
    expect(src).toContain("chip(t.verified_chip, 'verified')")
    // Candidates in the picking list carry the FOUND chip, never the verified chip.
    expect(src).toContain("chip(t.found_chip, 'found')")
    // The candidate <li> rendering block must not call the verified chip.
    const pickingBlock = src.slice(src.indexOf('data-entity-step="picking"'))
    expect(pickingBlock).not.toContain("chip(t.verified_chip, 'verified')")
  })

  it('the verified branch renders the authoritative registry name (verified.name), not a candidate name', () => {
    expect(src).toContain('data-verified-name')
    expect(src).toContain('verified.name')
  })
})

describe('VT-448 isValidGstinFormat (manual-entry format gate)', () => {
  it('accepts a well-formed 15-char GSTIN (any case / surrounding space)', () => {
    expect(isValidGstinFormat('27AAACR5055K1Z7')).toBe(true)
    expect(isValidGstinFormat(' 27aaacr5055k1z7 ')).toBe(true) // trimmed + upper-cased before test
  })

  it('rejects wrong length / shape', () => {
    expect(isValidGstinFormat('')).toBe(false)
    expect(isValidGstinFormat('27AAACR5055K1Z')).toBe(false) // 14 chars
    expect(isValidGstinFormat('27AAACR5055K1Z77')).toBe(false) // 16 chars
    expect(isValidGstinFormat('AB27AACR5055K1Z7')).toBe(false) // letters in the state-code slot
    expect(isValidGstinFormat('27AAACR5055K1A7')).toBe(false) // no mandatory Z in slot 14
  })

  it('is a FORMAT gate only — a valid format is NOT verification (Sandbox stays the authority)', () => {
    // A format-valid string never unlocks create on its own; canCreateAccount needs a verified entity.
    expect(canCreateAccount({ gstin: '27AAACR5055K1Z7', name: 'X' })).toBe(true)
    expect(canCreateAccount(null)).toBe(false)
  })
})
